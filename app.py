import os
import re
import time
import json
import uuid
import logging
import threading
from urllib.parse import quote
from flask import Flask, render_template_string, request, send_file, jsonify, after_this_request, redirect, Response, stream_with_context
import requests

import yt_dlp

app = Flask(__name__)

# প্রতিটা চলমান ডাউনলোডের অবস্থা (কত % হলো, শেষ হয়েছে কিনা, এরর কিনা)
# মেমোরিতে রাখা হয় যাতে ব্রাউজার প্রতি সেকেন্ডে চেক করে প্রগ্রেস বার
# আপডেট করতে পারে। এক Gunicorn worker ধরে রাখা হয়েছে (Dockerfile দেখো)
# যাতে সব রিকোয়েস্ট একই মেমোরিতে এই ডিকশনারি খুঁজে পায়।
DOWNLOAD_JOBS = {}

# ---------------------------------------------------------
# লগিং: টার্মিনালে + app.log ফাইলে আসল এরর সেভ হবে। কোনো
# ডাউনলোড/প্রিভিউ ফেইল করলে এই ফাইলটা চেক করলেই আসল কারণ বোঝা যাবে।
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("app.log", encoding="utf-8")],
)
logger = logging.getLogger("omor_downloader")

# Instagram/Facebook-এর কিছু লিংক (বিশেষ করে Reel) লগইন ছাড়া yt-dlp
# দিয়ে খোলা যায় না। এখানে cookies.txt ফাইলের পাথ দিলে সেই একাউন্ট
# দিয়ে লগইন অবস্থায় থাকা কন্টেন্টও ডাউনলোড করা যাবে।
# কীভাবে বানাবে: ব্রাউজারে "Get cookies.txt" এক্সটেনশন দিয়ে instagram.com/facebook.com
# থেকে এক্সপোর্ট করে app.py-এর পাশে cookies.txt নামে রাখো।
COOKIES_FILE = "cookies.txt"

# ---------------------------------------------------------
# CORS: ব্রাউজার থেকে সরাসরি এই সাইট ব্যবহার হবে (Render-এ হোস্ট করা
# একটা পাবলিক ওয়েবসাইট হিসেবে), তাই কোনো ক্রস-অরিজিন রিকোয়েস্ট
# ব্লক না হওয়ার জন্য হেডার যোগ করা হলো।
# ---------------------------------------------------------
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ---------------------------------------------------------
# Personal / Brand configuration — এখানে লিংক পরিবর্তন করলেই
# পুরো সাইটে আপডেট হয়ে যাবে, HTML-এর ভেতরে কিছু খোঁজার দরকার নেই।
# ---------------------------------------------------------
SITE_NAME = "OMOR DOWNLOADER"
WELCOME_TEXT = "Welcome to Omor Downloader"
FB_URL = "https://www.facebook.com/profile.php?id=61591653155991"
TG_URL = "https://t.me/omorcoding"

LOGO_PATH = "logo.png"  # app.py-এর পাশে এই নামে ছবিটা রাখতে হবে
DOWNLOAD_DIR = "downloads"

# ---------------------------------------------------------
# সিম্পল অ্যানালিটিক্স: মোট ডাউনলোড, কোন প্ল্যাটফর্মে কত, আর সাম্প্রতিক
# এরর — একটা JSON ফাইলে সেভ থাকে যাতে সার্ভার রিস্টার্ট হলেও হারিয়ে না
# যায়। শুধু হিডেন /admin প্যানেল থেকে দেখা যায় (পাসওয়ার্ড-গেটেড)।
# ---------------------------------------------------------
ANALYTICS_FILE = "analytics.json"
ANALYTICS_LOCK = threading.Lock()
ADMIN_PASSWORD = "omor2026"  # চাইলে এটা বদলে নাও

_DEFAULT_ANALYTICS = {
    "visits": 0,
    "downloads_by_platform": {"facebook": 0, "tiktok": 0, "instagram": 0, "pinterest": 0, "other": 0},
    "total_downloads": 0,
    "recent_errors": [],  # প্রতিটা: {time, source, url, message}
}


def load_analytics():
    with ANALYTICS_LOCK:
        if os.path.exists(ANALYTICS_FILE):
            try:
                with open(ANALYTICS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for k, v in _DEFAULT_ANALYTICS.items():
                        data.setdefault(k, v)
                    return data
            except Exception:
                pass
        return json.loads(json.dumps(_DEFAULT_ANALYTICS))


def save_analytics(data):
    with ANALYTICS_LOCK:
        try:
            with open(ANALYTICS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass


def platform_from_url(url: str) -> str:
    u = url.lower()
    if 'facebook.com' in u or 'fb.watch' in u:
        return 'facebook'
    if 'tiktok.com' in u:
        return 'tiktok'
    if 'instagram.com' in u:
        return 'instagram'
    if 'pinterest.com' in u or 'pin.it' in u:
        return 'pinterest'
    return 'other'


def record_visit():
    data = load_analytics()
    data['visits'] += 1
    save_analytics(data)


def record_download(url: str):
    data = load_analytics()
    p = platform_from_url(url)
    data['downloads_by_platform'][p] = data['downloads_by_platform'].get(p, 0) + 1
    data['total_downloads'] += 1
    save_analytics(data)


def record_error(source: str, url: str, message: str):
    data = load_analytics()
    data['recent_errors'].insert(0, {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'source': source,
        'url': url[:200],
        'message': str(message)[:200],
    })
    data['recent_errors'] = data['recent_errors'][:30]  # সাম্প্রতিক ৩০টাই যথেষ্ট
    save_analytics(data)


HTML_CODE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OMOR DOWNLOADER</title>
    <link rel="manifest" href="/manifest.json">
    <link rel="apple-touch-icon" href="/app_icon">
    <meta name="theme-color" content="#05060a">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #05060a;
            --bg-grad: radial-gradient(circle at 20% -10%, #0d3b3f 0%, #05060a 45%), radial-gradient(circle at 100% 0%, #2a0d3f 0%, transparent 40%);
            --box: rgba(255,255,255,0.05);
            --box-border: rgba(0,255,255,0.18);
            --text: #f2f2f2;
            --muted: #9aa3ad;
            --accent: #00e5ff;
            --accent2: #a742ff;
            --status: #ff3b5c;
            --success: #23d18b;
        }
        body.light-mode {
            --bg: #eef1f5;
            --bg-grad: radial-gradient(circle at 20% -10%, #dff3ff 0%, #eef1f5 45%);
            --box: rgba(255,255,255,0.75);
            --box-border: rgba(0,140,255,0.25);
            --text: #1c1c1e;
            --muted: #5c6570;
            --accent: #0077ff;
            --accent2: #7a2bff;
            --status: #e0304f;
            --success: #1aa971;
        }

        * { box-sizing: border-box; }
        body {
            background: var(--bg-grad), var(--bg);
            background-attachment: fixed;
            color: var(--text);
            font-family: 'Poppins', 'Segoe UI', sans-serif;
            text-align: center;
            margin: 0;
            padding: 0 0 60px 0;
            min-height: 100vh;
            transition: background 0.4s, color 0.4s;
        }

        /* ---------- Top header bar ---------- */
        .topbar {
            position: sticky;
            top: 0;
            z-index: 300;
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 16px 20px;
            background: rgba(10, 12, 18, 0.55);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border-bottom: 1px solid rgba(255,255,255,0.08);
        }
        body.light-mode .topbar { background: rgba(255,255,255,0.65); border-bottom: 1px solid rgba(0,0,0,0.06); }
        .brand { display: flex; align-items: center; gap: 10px; }
        .brand-name {
            font-weight: 800;
            font-size: 1.05em;
            letter-spacing: 0.5px;
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
        }
        .theme-toggle {
            cursor: pointer;
            font-size: 1.2em;
            color: var(--accent);
            width: 38px; height: 38px;
            display: flex; align-items: center; justify-content: center;
            border-radius: 50%;
            background: var(--box);
            border: 1px solid var(--box-border);
        }
        .install-btn {
            display: flex; align-items: center; gap: 6px;
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            color: #05060a; border: none; border-radius: 999px;
            padding: 8px 14px 8px 8px; font-size: 0.78em; font-weight: 700;
            cursor: pointer; font-family: inherit;
        }
        .install-btn img { width: 20px; height: 20px; border-radius: 5px; object-fit: cover; }

        /* ---------- Hero / logo ---------- */
        .hero { padding: 34px 20px 10px; }
        .logo-wrap {
            width: 120px; height: 120px;
            margin: 0 auto 14px;
            border-radius: 32px;
            padding: 4px;
            background: linear-gradient(135deg, var(--accent), var(--accent2));
            box-shadow: 0 0 30px rgba(0,229,255,0.35);
        }
        .logo-wrap img {
            width: 100%; height: 100%;
            object-fit: cover;
            border-radius: 28px;
            display: block;
            background: #0b0e14;
        }
        .welcome-text {
            font-size: 1.15em;
            font-weight: 600;
            color: var(--text);
            margin: 0 0 4px;
        }
        .sub-text {
            font-size: 0.82em;
            color: var(--muted);
            margin: 0 0 22px;
        }

        /* ---------- Main card ---------- */
        .main-box {
            background: var(--box);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            padding: 24px;
            border-radius: 24px;
            max-width: 420px;
            margin: 0 auto;
            border: 1px solid var(--box-border);
            position: relative;
            box-shadow: 0 20px 45px rgba(0,0,0,0.35);
        }

        .input-group { position: relative; margin-bottom: 16px; display: flex; align-items: center; }
        input[type="text"] {
            width: 100%; padding: 16px 46px 16px 16px;
            border-radius: 14px;
            border: 1px solid var(--box-border);
            background: rgba(255,255,255,0.04);
            color: var(--text);
            outline: none;
            font-size: 0.95em;
            box-sizing: border-box;
            transition: border-color 0.2s, box-shadow 0.2s;
        }
        input[type="text"]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0,229,255,0.15); }
        .paste-btn {
            position: absolute; right: 12px; color: var(--accent);
            cursor: pointer; font-size: 1.15em; background: transparent; border: none;
        }
        .clear-btn {
            position: absolute; right: 12px; color: var(--status);
            cursor: pointer; font-size: 1.05em; background: transparent; border: none;
            display: none;
        }

        #thumbnail-area {
            display: none; margin: 4px 0 16px; border-radius: 14px;
            overflow: hidden; border: 1px solid var(--box-border);
            background: #000; position: relative;
        }
        #thumb-img { width: 100%; display: block; opacity: 0.75; }
        #video-info {
            position: absolute; bottom: 0; left: 0; right: 0;
            background: linear-gradient(0deg, rgba(0,0,0,0.9), transparent);
            padding: 12px 10px 8px; font-size: 12px; text-align: left;
        }
        .skeleton {
            width: 100%; aspect-ratio: 16/9;
            background: linear-gradient(90deg, rgba(255,255,255,0.06) 25%, rgba(255,255,255,0.14) 37%, rgba(255,255,255,0.06) 63%);
            background-size: 400% 100%;
            animation: shimmer 1.4s ease infinite;
        }
        @keyframes shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0% 50%; } }
        .preview-play-btn {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            width: 54px; height: 54px; border-radius: 50%;
            background: rgba(0,0,0,0.55); border: 2px solid rgba(255,255,255,0.8);
            color: #fff; font-size: 20px; display: flex; align-items: center; justify-content: center;
            cursor: pointer; backdrop-filter: blur(2px);
        }
        #preview-video {
            width: 100%; display: none; background: #000; max-height: 320px;
        }

        select {
            width: 100%; padding: 14px; border-radius: 14px;
            background: rgba(255,255,255,0.04); color: var(--text);
            border: 1px solid var(--box-border); margin-bottom: 16px; outline: none;
            font-size: 0.92em;
        }

        .start-btn {
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            color: #05060a;
            border: none;
            padding: 16px;
            border-radius: 14px;
            width: 100%;
            font-weight: 700;
            cursor: pointer;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-size: 0.92em;
            transition: transform 0.15s, box-shadow 0.2s;
            box-shadow: 0 10px 25px rgba(0,229,255,0.25);
        }
        .start-btn:hover { transform: translateY(-2px); box-shadow: 0 14px 30px rgba(0,229,255,0.35); }
        .start-btn:active { transform: translateY(0); }
        .start-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

        #progress-overlay {
            display: none; position: absolute; top:0; left:0; width:100%; height:100%;
            background: rgba(5,6,10,0.92); backdrop-filter: blur(6px);
            border-radius: 24px; z-index: 100;
            flex-direction: column; justify-content: center; align-items: center;
            padding: 20px;
        }
        .spinner {
            width: 42px; height: 42px; border: 4px solid rgba(255,255,255,0.15);
            border-top: 4px solid var(--accent); border-radius: 50%;
            animation: spin 0.9s linear infinite;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        .progress-track {
            width: 100%; max-width: 220px; height: 10px; border-radius: 999px;
            background: rgba(255,255,255,0.12); overflow: hidden; margin-top: 16px;
        }
        .progress-fill {
            height: 100%; width: 0%; border-radius: 999px;
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            transition: width 0.3s ease;
        }
        #progress-percent {
            color: var(--accent); font-weight: 700; font-size: 0.9em; margin-top: 10px;
        }
        #progress-status-text {
            color: var(--muted); font-size: 0.78em; margin-top: 2px;
        }

        #error-msg {
            color: var(--status); font-size: 13px; margin-top: 10px; display: none;
            background: rgba(255,59,92,0.1); padding: 8px 10px; border-radius: 10px;
        }

        /* ---------- Download history ---------- */
        .history-toggle {
            margin-top: 18px; text-align: center; font-size: 0.85em;
            color: var(--muted); cursor: pointer; user-select: none;
        }
        .history-toggle:hover { color: var(--accent); }
        #history-panel {
            display: none; margin-top: 12px; text-align: left;
            max-height: 260px; overflow-y: auto;
        }
        .history-item {
            display: flex; align-items: center; gap: 10px;
            padding: 8px; border-radius: 12px;
            background: rgba(255,255,255,0.03); margin-bottom: 8px;
            cursor: pointer; border: 1px solid transparent;
        }
        .history-item:hover { border-color: var(--box-border); }
        .history-item img {
            width: 46px; height: 46px; object-fit: cover; border-radius: 8px;
            background: #000; flex-shrink: 0;
        }
        .history-item .hi-text { flex: 1; min-width: 0; }
        .history-item .hi-title {
            font-size: 0.8em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        }
        .history-item .hi-meta { font-size: 0.68em; color: var(--muted); }
        .history-empty { font-size: 0.8em; color: var(--muted); text-align: center; padding: 10px 0; }
        .history-clear {
            display: block; margin: 8px auto 0; font-size: 0.75em;
            color: var(--status); background: none; border: none; cursor: pointer;
        }

        .platforms {
            display: flex; justify-content: center; gap: 12px; margin-top: 22px; flex-wrap: wrap;
        }
        .platform-badge {
            width: 40px; height: 40px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            font-size: 16px; color: #fff !important;
            transition: transform 0.2s;
        }
        .platform-badge:hover { transform: translateY(-3px); }
        .platform-badge.fb { background: #1877f2; }
        .platform-badge.tk { background: #000; border: 1px solid #888; }
        .platform-badge.ig { background: linear-gradient(45deg, #f09433, #bc1888); }
        .platform-badge.yt { background: #ff0000; }
        .platform-badge.pin { background: #E60023; }

        /* ---------- Floating social icons ---------- */
        .fab-stack {
            position: fixed; right: 18px; bottom: 22px; z-index: 400;
            display: flex; flex-direction: column; gap: 12px;
        }
        .fab {
            width: 52px; height: 52px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center;
            color: #fff; text-decoration: none; font-size: 20px;
            box-shadow: 0 10px 20px rgba(0,0,0,0.35);
            transition: transform 0.2s;
        }
        .fab:hover { transform: scale(1.08); }
        .fab.fb { background: #1877f2; }
        .fab.tg { background: #229ED9; }

        .footer-credit {
            margin-top: 26px; font-size: 0.78em; color: var(--muted);
            display: flex; align-items: center; justify-content: center; gap: 6px;
        }
        .footer-credit b {
            background: linear-gradient(90deg, var(--accent), var(--accent2));
            -webkit-background-clip: text; background-clip: text; color: transparent;
        }

        @media (max-width: 420px) {
            .main-box { margin: 0 16px; }
        }
    </style>
</head>
<body onload="requestNotify()">

    <div class="topbar">
        <div class="brand">
            <span class="brand-name">SITE_NAME_PLACEHOLDER</span>
        </div>
        <div style="display:flex; align-items:center; gap:10px;">
            <button id="install-btn" class="install-btn" onclick="installApp()" style="display:none;">
                <img src="/app_icon" alt="App"> Install App
            </button>
            <div class="theme-toggle" onclick="toggleTheme()">
                <i class="fas fa-moon" id="theme-icon"></i>
            </div>
        </div>
    </div>

    <div class="hero">
        <div class="logo-wrap">
            <img src="/my_photo" alt="Omor Downloader">
        </div>
        <p class="welcome-text">WELCOME_TEXT_PLACEHOLDER</p>
        <p class="sub-text">Facebook • TikTok • Instagram • Pinterest — video &amp; photo downloader</p>
    </div>

    <div class="main-box">
        <div id="progress-overlay">
            <div class="spinner"></div>
            <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
            <div id="progress-percent">0%</div>
            <div id="progress-status-text">Starting...</div>
        </div>

        <div class="input-group">
            <input type="text" id="url" placeholder="Paste video/photo link here..." oninput="handleInputChange()" onpaste="setTimeout(function(){ toggleInputButtons(); fetchPreview(true); }, 0)">
            <button class="fas fa-paste paste-btn" id="paste-btn" onclick="handlePaste()"></button>
            <button class="fas fa-xmark clear-btn" id="clear-btn" onclick="clearUrl()"></button>
        </div>

        <div id="thumbnail-area">
            <img id="thumb-img" src="">
            <div class="preview-play-btn" id="preview-play-btn" onclick="playPreview()" style="display:none;">
                <i class="fas fa-play"></i>
            </div>
            <video id="preview-video" controls></video>
            <div id="video-info">
                <b id="video-title"></b><br>
                <span id="video-meta" style="color: var(--accent);"></span>
            </div>
        </div>

        <select id="quality">
            <option value="best">Best Quality</option>
            <option value="720">HD (720p)</option>
            <option value="480">SD (480p)</option>
            <option value="mp3">Audio (MP3)</option>
        </select>

        <button onclick="initiateDownload()" class="start-btn" id="dl-btn">Start Download</button>
        <div id="error-msg"></div>

        <div class="history-toggle" onclick="toggleHistory()">
            <i class="fas fa-clock-rotate-left"></i> <span id="history-toggle-text">Recent Downloads</span>
        </div>
        <div id="history-panel"></div>

        <div class="platforms">
            <span class="platform-badge fb"><i class="fab fa-facebook-f"></i></span>
            <span class="platform-badge tk"><i class="fab fa-tiktok"></i></span>
            <span class="platform-badge ig"><i class="fab fa-instagram"></i></span>
            <span class="platform-badge pin"><i class="fab fa-pinterest-p"></i></span>
        </div>

        <div class="footer-credit">
            <i class="fas fa-code"></i> Developed by <b>Omor Coding</b>
        </div>
    </div>

    <div class="fab-stack">
        <a href="FB_URL_PLACEHOLDER" target="_blank" class="fab fb" title="Facebook"><i class="fab fa-facebook-f"></i></a>
        <a href="TG_URL_PLACEHOLDER" target="_blank" class="fab tg" title="Telegram"><i class="fab fa-telegram-plane"></i></a>
    </div>

    <script>
        function requestNotify() { if (typeof Notification !== 'undefined' && Notification.permission !== 'granted') Notification.requestPermission(); }

        // ---------- PWA Install (Add to Home Screen) ----------
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.register('/sw.js').catch(function(){});
        }
        let deferredInstallPrompt = null;
        window.addEventListener('beforeinstallprompt', function(e) {
            e.preventDefault();
            deferredInstallPrompt = e;
            document.getElementById('install-btn').style.display = 'flex';
        });
        function installApp() {
            const btn = document.getElementById('install-btn');
            if (deferredInstallPrompt) {
                deferredInstallPrompt.prompt();
                deferredInstallPrompt.userChoice.finally(function() {
                    deferredInstallPrompt = null;
                    btn.style.display = 'none';
                });
            } else {
                alert('আপনার ব্রাউজারের মেনু থেকে "Add to Home screen" / "Install app" অপশনটি ব্যবহার করুন।');
            }
        }
        window.addEventListener('appinstalled', function() {
            document.getElementById('install-btn').style.display = 'none';
        });
        function toggleTheme() {
            document.body.classList.toggle('light-mode');
            const icon = document.getElementById('theme-icon');
            icon.className = document.body.classList.contains('light-mode') ? 'fas fa-sun' : 'fas fa-moon';
        }
        function showError(msg) {
            const el = document.getElementById('error-msg');
            el.innerText = msg;
            el.style.display = 'block';
        }
        function clearError() {
            document.getElementById('error-msg').style.display = 'none';
        }

        async function handlePaste() {
            try {
                const text = await navigator.clipboard.readText();
                if (text) {
                    document.getElementById('url').value = text;
                    toggleInputButtons();
                    fetchPreview(true); // পেস্ট মানেই সম্পূর্ণ লিংক, তাই সাথে সাথেই প্রিভিউ আনো
                }
            } catch (e) { alert("Please allow clipboard access or paste manually."); }
        }

        function clearUrl() {
            document.getElementById('url').value = '';
            document.getElementById('thumbnail-area').style.display = 'none';
            const video = document.getElementById('preview-video');
            video.pause();
            video.style.display = 'none';
            document.getElementById('preview-play-btn').style.display = 'none';
            document.getElementById('thumb-img').style.display = 'block';
            lastPreviewHasVideo = false;
            clearError();
            document.getElementById('url').focus();
            toggleInputButtons();
        }

        function toggleInputButtons() {
            const hasText = document.getElementById('url').value.trim().length > 0;
            document.getElementById('paste-btn').style.display = hasText ? 'none' : 'block';
            document.getElementById('clear-btn').style.display = hasText ? 'block' : 'none';
        }

        function handleInputChange() {
            toggleInputButtons();
            fetchPreview(false);
        }

        let previewTimer = null;
        let lastPreviewedUrl = null;
        let lastPreviewOk = false;
        let lastPreviewHasVideo = false;
        function fetchPreview(immediate) {
            clearTimeout(previewTimer);
            const run = async () => {
                const url = document.getElementById('url').value.trim();
                clearError();
                if (url.length < 8) return;
                lastPreviewOk = false;
                lastPreviewHasVideo = false;
                document.getElementById('thumbnail-area').style.display = 'block';
                const img = document.getElementById('thumb-img');
                img.src = '';
                img.classList.add('skeleton');
                document.getElementById('preview-play-btn').style.display = 'none';
                document.getElementById('preview-video').style.display = 'none';
                document.getElementById('preview-video').pause && document.getElementById('preview-video').pause();
                document.getElementById('video-title').innerText = 'Loading preview...';
                document.getElementById('video-meta').innerText = '';
                try {
                    const res = await fetch('/get_info?url=' + encodeURIComponent(url));
                    const data = await res.json();
                    img.classList.remove('skeleton');
                    if (data.error) {
                        document.getElementById('thumbnail-area').style.display = 'none';
                        showError('Could not load preview: ' + data.error);
                        return;
                    }
                    document.getElementById('thumbnail-area').style.display = 'block';
                    img.style.display = 'block';
                    img.src = data.thumbnail || '';
                    document.getElementById('video-title').innerText = data.title || 'Untitled';
                    document.getElementById('video-meta').innerText = `Duration: ${data.duration} | Size: ~${data.size}`;
                    // ডাউনলোডের আগে একবার ভিডিওটা দেখে নেওয়ার জন্য প্লে বাটন
                    if (data.has_preview) {
                        lastPreviewHasVideo = true;
                        document.getElementById('preview-play-btn').style.display = 'flex';
                    }
                    // এই URL-টা ইতিমধ্যে ভ্যালিড প্রমাণিত — ডাউনলোডের সময় আবার
                    // চেক না করে সরাসরি ডাউনলোড শুরু করার জন্য মনে রাখা হলো
                    lastPreviewedUrl = url;
                    lastPreviewOk = true;
                } catch (e) {
                    img.classList.remove('skeleton');
                    showError('Network error while fetching preview.');
                }
            };
            if (immediate) {
                run();
            } else {
                previewTimer = setTimeout(run, 500); // টাইপ করার সময় প্রতি কী-স্ট্রোকে যাতে সার্ভারে না যায়
            }
        }

        async function playPreview() {
            if (!lastPreviewHasVideo || !lastPreviewedUrl) return;
            const video = document.getElementById('preview-video');
            const img = document.getElementById('thumb-img');
            const playBtn = document.getElementById('preview-play-btn');
            const streamUrl = '/stream_preview?url=' + encodeURIComponent(lastPreviewedUrl);

            playBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; // চেক করার সময় লোডিং দেখানো

            try {
                // আগে হেড-এর মতো একটা চেক করে দেখা হচ্ছে সার্ভার আসল ভিডিও পাঠাচ্ছে
                // নাকি এরর — এতে ব্যর্থ হলে আসল কারণটা দেখানো যায়, শুধু "চালানো
                // যাচ্ছে না" বলার বদলে
                const check = await fetch(streamUrl, { headers: { 'Range': 'bytes=0-1' } });
                const ct = check.headers.get('Content-Type') || '';
                if (ct.includes('application/json')) {
                    const data = await check.json();
                    showError('প্রিভিউ চালানো যায়নি: ' + (data.error || 'Unknown error') + ' — তবে ডাউনলোড করা যাবে।');
                    playBtn.innerHTML = '<i class="fas fa-play"></i>';
                    return;
                }
            } catch (e) {
                showError('প্রিভিউ চেক করার সময় নেটওয়ার্ক এরর হয়েছে।');
                playBtn.innerHTML = '<i class="fas fa-play"></i>';
                return;
            }

            video.src = streamUrl;
            video.style.display = 'block';
            img.style.display = 'none';
            playBtn.style.display = 'none';
            playBtn.innerHTML = '<i class="fas fa-play"></i>';
            video.play().catch(() => {
                showError('এই ভিডিওটা প্রিভিউ চালানো যাচ্ছে না, সরাসরি ডাউনলোড করে দেখুন।');
                video.style.display = 'none';
                img.style.display = 'block';
            });
            video.onerror = function() {
                showError('প্রিভিউ চালানো যায়নি, তবে ডাউনলোড করা যাবে।');
                video.style.display = 'none';
                img.style.display = 'block';
            };
        }

        async function initiateDownload() {
            const url = document.getElementById('url').value.trim();
            const q = document.getElementById('quality').value;
            clearError();
            if (!url) return showError('Please paste a link!');

            const btn = document.getElementById('dl-btn');
            btn.disabled = true;
            const overlay = document.getElementById('progress-overlay');
            const fill = document.getElementById('progress-fill');
            const percentText = document.getElementById('progress-percent');
            const statusText = document.getElementById('progress-status-text');
            fill.style.width = '0%';
            percentText.innerText = '0%';
            statusText.innerText = 'Starting...';
            overlay.style.display = 'flex';

            const finish = () => {
                btn.disabled = false;
                overlay.style.display = 'none';
            };

            try {
                const startRes = await fetch(`/start_download?url=${encodeURIComponent(url)}&q=${q}`);
                const startData = await startRes.json();
                if (startData.error) {
                    showError(startData.error);
                    finish();
                    return;
                }
                const jobId = startData.job_id;

                const poll = async () => {
                    try {
                        const res = await fetch('/progress/' + jobId);
                        const data = await res.json();
                        if (data.error) {
                            showError(data.error);
                            finish();
                            return;
                        }
                        const pct = data.percent || 0;
                        fill.style.width = pct + '%';
                        percentText.innerText = pct + '%';
                        statusText.innerText = data.status === 'downloading' ? 'Downloading...'
                            : data.status === 'starting' ? 'Starting...'
                            : data.status === 'processing' ? 'Processing...'
                            : data.status === 'retrying' ? 'একবার ফেইল হয়েছে, আবার চেষ্টা করা হচ্ছে...'
                            : data.status;

                        if (data.status === 'finished') {
                            statusText.innerText = 'Done! Saving file...';
                            if (navigator.vibrate) navigator.vibrate(200); // হ্যাপটিক ফিডব্যাক
                            // থাম্বনেইল/টাইটেল যা ইতিমধ্যে দেখানো আছে সেটা হিস্টোরিতে সেভ করা
                            saveToHistory({
                                url: url,
                                title: document.getElementById('video-title').innerText || 'Untitled',
                                thumbnail: document.getElementById('thumb-img').src || '',
                                quality: q,
                                time: Date.now()
                            });
                            window.location.href = '/fetch_file/' + jobId;
                            if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
                                new Notification("OMOR DOWNLOADER", { body: "Download complete!", icon: "/my_photo" });
                            }
                            finish();
                            return;
                        }
                        if (data.status === 'error') {
                            if (navigator.vibrate) navigator.vibrate([100, 50, 100]); // এরর হ্যাপটিক প্যাটার্ন
                            showError(data.error || 'Download failed');
                            finish();
                            return;
                        }
                        setTimeout(poll, 800);
                    } catch (e) {
                        showError('Lost connection while checking progress.');
                        finish();
                    }
                };
                poll();
            } catch (e) {
                showError('Could not reach the server. Check your connection and try again.');
                finish();
            }
        }

        // ---------- Download history (browser-এ localStorage-এ সেভ থাকে) ----------
        const HISTORY_KEY = 'omor_download_history';
        const HISTORY_MAX = 10;

        function loadHistory() {
            try {
                return JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
            } catch (e) {
                return [];
            }
        }

        function saveToHistory(entry) {
            let list = loadHistory();
            list = list.filter(item => item.url !== entry.url); // ডুপ্লিকেট সরানো
            list.unshift(entry);
            list = list.slice(0, HISTORY_MAX);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
            if (document.getElementById('history-panel').style.display !== 'none') {
                renderHistory();
            }
        }

        function renderHistory() {
            const list = loadHistory();
            const panel = document.getElementById('history-panel');
            if (list.length === 0) {
                panel.innerHTML = '<div class="history-empty">No downloads yet</div>';
                return;
            }
            let html = '';
            list.forEach((item, idx) => {
                const safeTitle = (item.title || 'Untitled').replace(/</g, '&lt;');
                html += `<div class="history-item" onclick="reuseHistoryItem(${idx})">
                    <img src="${item.thumbnail}" onerror="this.style.display='none'">
                    <div class="hi-text">
                        <div class="hi-title">${safeTitle}</div>
                        <div class="hi-meta">${new Date(item.time).toLocaleDateString()} • ${item.quality}</div>
                    </div>
                    <i class="fas fa-rotate-right" style="color:var(--accent);"></i>
                </div>`;
            });
            html += '<button class="history-clear" onclick="clearHistory()">Clear history</button>';
            panel.innerHTML = html;
        }

        function reuseHistoryItem(idx) {
            const list = loadHistory();
            const item = list[idx];
            if (!item) return;
            document.getElementById('url').value = item.url;
            toggleInputButtons();
            fetchPreview(true);
            document.getElementById('history-panel').style.display = 'none';
            document.getElementById('history-toggle-text').innerText = 'Recent Downloads';
        }

        function clearHistory() {
            localStorage.removeItem(HISTORY_KEY);
            renderHistory();
        }

        function toggleHistory() {
            const panel = document.getElementById('history-panel');
            const isHidden = panel.style.display === 'none' || !panel.style.display;
            if (isHidden) {
                renderHistory();
                panel.style.display = 'block';
                document.getElementById('history-toggle-text').innerText = 'Hide History';
            } else {
                panel.style.display = 'none';
                document.getElementById('history-toggle-text').innerText = 'Recent Downloads';
            }
        }

        // ---------- Share Target (অন্য অ্যাপ থেকে "Share" করলে লিংক অটো-ফিল হবে) ----------
        (function checkSharedLink() {
            const params = new URLSearchParams(window.location.search);
            const shared = params.get('prefill');
            if (shared) {
                document.getElementById('url').value = shared;
                toggleInputButtons();
                fetchPreview(true);
                // URL পরিষ্কার করা হলো যাতে রিফ্রেশে আবার প্রিফিল না হয়
                window.history.replaceState({}, document.title, window.location.pathname);
            }
        })();
    </script>
</body>
</html>
"""


def sanitize_filename(name: str) -> str:
    """Strip characters that break filesystems / HTTP headers."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name.strip()[:150] or "video"


@app.route('/')
def home():
    record_visit()
    return render_template_string(
        HTML_CODE
        .replace("SITE_NAME_PLACEHOLDER", SITE_NAME)
        .replace("WELCOME_TEXT_PLACEHOLDER", WELCOME_TEXT)
        .replace("FB_URL_PLACEHOLDER", FB_URL)
        .replace("TG_URL_PLACEHOLDER", TG_URL)
    )


@app.route('/my_photo')
def my_photo():
    paths = [LOGO_PATH, "logo.png", "faruk.jpg", "/sdcard/Download/logo.png", "static/logo.png"]
    for p in paths:
        if os.path.exists(p):
            return send_file(p)
    return jsonify({"error": "Logo not found"}), 404


@app.route('/app_icon')
def app_icon():
    paths = ["app-icon.png", "static/app-icon.png"]
    for p in paths:
        if os.path.exists(p):
            return send_file(p)
    return jsonify({"error": "App icon not found"}), 404


@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "Omor Downloader",
        "short_name": "Omor DL",
        "description": "Facebook, TikTok, Instagram & Pinterest video/photo downloader",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#05060a",
        "theme_color": "#05060a",
        "icons": [
            {"src": "/app_icon", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/app_icon", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ],
        # অ্যাপ ইনস্টল করা থাকলে Facebook/Instagram/TikTok অ্যাপ থেকে
        # সরাসরি "Share" চাপলে এই সাইট শেয়ার-টার্গেট লিস্টে দেখাবে।
        "share_target": {
            "action": "/share-target",
            "method": "GET",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url"
            }
        }
    })


@app.route('/sw.js')
def service_worker():
    # খুবই সাধারণ একটা service worker, শুধু PWA ইনস্টলযোগ্যতার শর্ত পূরণ করার জন্য
    js = "self.addEventListener('fetch', function(e) {});"
    return app.response_class(js, mimetype='application/javascript')


@app.route('/share-target')
def share_target():
    # অনেক অ্যাপ (Instagram/Facebook) লিংকটা 'text' ফিল্ডে পাঠায়,
    # 'url' ফিল্ডে সবসময় না-ও থাকতে পারে — তাই দুটোই চেক করা হচ্ছে।
    shared_url = request.args.get('url', '')
    shared_text = request.args.get('text', '')
    candidate = f"{shared_url} {shared_text}"
    match = re.search(r'https?://\S+', candidate)
    found_url = match.group(0) if match else ''
    if found_url:
        return redirect('/?prefill=' + quote(found_url, safe=''))
    return redirect('/')


# YouTube এখন সাপোর্ট করা হয় না — ইউজারকে ভুল ধারণা না দিয়ে সাথে সাথেই
# পরিষ্কার মেসেজ দেখানোর জন্য
def is_youtube_url(url: str) -> bool:
    u = url.lower()
    return 'youtube.com' in u or 'youtu.be' in u


def _find_playable_format(info: dict):
    """
    <video> ট্যাগে সরাসরি বাজানো যাবে এমন একটা ফরম্যাট খুঁজে বের করে।
    TikTok সবসময় 'formats' লিস্ট পপুলেট করে, কিন্তু Facebook/Instagram/Pinterest-এর
    ক্ষেত্রে yt-dlp অনেক সময় 'formats' লিস্ট খালি রেখে সরাসরি top-level info
    dict-এ ('url', 'vcodec' ইত্যাদি) একটামাত্র ফরম্যাট রাখে — তাই দুই জায়গাতেই
    খোঁজা হচ্ছে।
    """
    def is_playable(f):
        return (
            f.get('vcodec') not in (None, 'none')
            and f.get('acodec') not in (None, 'none')
            and f.get('url')
            and f.get('protocol') not in ('m3u8', 'm3u8_native', 'http_dash_segments')
        )

    formats = info.get('formats') or []
    playable = [f for f in formats if is_playable(f)]
    if playable:
        playable.sort(key=lambda f: f.get('height') or 9999)
        return playable[0]

    # ফলব্যাক: top-level info dict-টাকেই একটা ফরম্যাটের মতো ট্রিট করা
    if is_playable(info):
        return info

    return None


@app.route('/get_info')
def get_info():
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400

    if is_youtube_url(url):
        return jsonify({'error': 'YouTube is not supported on this site. Please use Facebook, TikTok, Instagram, or Pinterest links.'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': 10,
            'retries': 1,
            'extractor_retries': 1,
            # একটামাত্র client (web) দিয়ে অনেক সময় কিছু ভিডিওর জন্য কোনো
            # ব্যবহারযোগ্য ফরম্যাট পাওয়া যায় না। একাধিক client একসাথে
            # চেষ্টা করলে সাফল্যের সম্ভাবনা অনেক বেড়ে যায়।
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts['cookiefile'] = COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        filesize = info.get('filesize_approx') or info.get('filesize') or 0
        size_mb = f"{round(filesize / 1_000_000, 2)} MB" if filesize else "N/A"
        duration = time.strftime('%M:%S', time.gmtime(info.get('duration') or 0))
        title = (info.get('title') or 'Untitled')[:60]

        # ডাউনলোডের আগে ছোট প্লেয়ারে দেখে নেওয়া যাবে কিনা তা চেক করা হচ্ছে —
        # আসল CDN লিংক এখানে পাঠানো হয় না (browser সরাসরি সেখানে গেলে referrer/token
        # ব্লক করে দেয়), শুধু "প্রিভিউ সম্ভব কিনা" জানানো হচ্ছে। প্রকৃত ভিডিও
        # /stream_preview রুট দিয়ে আমাদের সার্ভার হয়ে প্রক্সি করে পাঠানো হবে।
        has_preview = _find_playable_format(info) is not None

        return jsonify({
            'thumbnail': info.get('thumbnail'),
            'title': title,
            'duration': duration,
            'size': size_mb,
            'has_preview': has_preview,
        })
    except yt_dlp.utils.DownloadError as e:
        logger.error("get_info DownloadError for %s: %s", url, e)
        msg = str(e)
        low = msg.lower()
        if 'login' in low or 'rate-limit' in low or 'private' in low or 'sign in' in low or 'bot' in low:
            err = 'This platform is blocking the server (needs login/cookies or is rate-limiting). Add a cookies.txt file. Details: ' + msg[:150]
        else:
            err = 'Invalid or unsupported link. Details: ' + msg[:150]
        record_error('get_info', url, msg[:200])
        return jsonify({'error': err}), 400
    except Exception as e:
        logger.exception("get_info unexpected error for %s", url)
        record_error('get_info', url, str(e)[:200])
        return jsonify({'error': f'Server error: {e}'}), 500


@app.route('/stream_preview')
def stream_preview():
    """
    ব্রাউজার সরাসরি Facebook/Instagram/TikTok-এর CDN লিংকে গেলে referrer/token
    চেক করে ব্লক করে দেয় (তাই আগে প্রিভিউ প্লেয়ার কাজ করছিল না)। এই রুট
    সার্ভার থেকে (yt-dlp যেই হেডার ব্যবহার করে সেটা দিয়েই) ভিডিওটা fetch করে
    ব্রাউজারে "প্রক্সি" করে পাঠায়, ফলে ব্রাউজার শুধু আমাদের নিজের ডোমেইনের
    সাথে কথা বলে — CDN-এর ব্লকিং এড়ানো যায়।
    """
    url = request.args.get('url', '').strip()
    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if is_youtube_url(url):
        return jsonify({'error': 'YouTube is not supported on this site.'}), 400

    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'socket_timeout': 10,
        }
        if os.path.exists(COOKIES_FILE):
            ydl_opts['cookiefile'] = COOKIES_FILE

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        chosen = _find_playable_format(info)
        if not chosen:
            return jsonify({'error': 'No playable preview stream found for this link'}), 404

        direct_url = chosen['url']
        # yt-dlp প্রতিটা ফরম্যাটের সাথে দরকারি হেডার বেঁধে দেয় (এগুলো ছাড়া
        # অনেক CDN রিকোয়েস্ট রিজেক্ট করে দেয়) — শুধু জরুরি হেডারগুলোই ফরওয়ার্ড
        # করা হচ্ছে, বাকিগুলো (Cookie, Host ইত্যাদি) বাদ দেওয়া হচ্ছে যাতে
        # requests লাইব্রেরির নিজস্ব কানেকশন হ্যান্ডলিং-এর সাথে দ্বন্দ্ব না হয়
        source_headers = chosen.get('http_headers') or info.get('http_headers') or {}
        upstream_headers = {}
        for key in ('User-Agent', 'Referer', 'Origin', 'Accept', 'Accept-Language'):
            if source_headers.get(key):
                upstream_headers[key] = source_headers[key]
        upstream_headers.setdefault('User-Agent', 'Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36')

        range_header = request.headers.get('Range')
        if range_header:
            upstream_headers['Range'] = range_header

        upstream = requests.get(direct_url, headers=upstream_headers, stream=True, timeout=20)
        logger.info("stream_preview upstream status=%s for %s", upstream.status_code, url)

        if upstream.status_code >= 400:
            record_error('stream_preview', url, f'upstream status {upstream.status_code}')
            return jsonify({'error': f'Preview source rejected the request (status {upstream.status_code})'}), 502

        # শুধু 'content-type' বাদে বাকিগুলো ফরওয়ার্ড করা হচ্ছে, কারণ Content-Type
        # নিচে content_type= প্যারামিটার দিয়ে আলাদাভাবে সেট করা হচ্ছে — দুই জায়গায়
        # একসাথে থাকলে ডুপ্লিকেট হেডার তৈরি হয়ে ব্রাউজার ভিডিওটা রিজেক্ট করে দেয়
        pass_through = ('content-length', 'content-range', 'accept-ranges')
        resp_headers = [(k, v) for k, v in upstream.headers.items() if k.lower() in pass_through]

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=65536)),
            status=upstream.status_code,
            headers=resp_headers,
            content_type=upstream.headers.get('Content-Type', 'video/mp4'),
        )
    except yt_dlp.utils.DownloadError as e:
        logger.error("stream_preview DownloadError for %s: %s", url, e)
        record_error('stream_preview', url, str(e)[:200])
        return jsonify({'error': 'Could not load preview for this link'}), 400
    except Exception as e:
        logger.exception("stream_preview unexpected error for %s", url)
        record_error('stream_preview', url, str(e)[:200])
        return jsonify({'error': f'Preview server error: {e}'}), 500


def _map_download_error(e) -> str:
    """DownloadError-এর টেক্সট দেখে ইউজার-ফ্রেন্ডলি মেসেজ বানায়।"""
    msg = str(e)
    low = msg.lower()
    if 'login' in low or 'rate-limit' in low or 'private' in low or 'sign in' in low or 'bot' in low:
        return 'This platform is blocking the server (needs login/cookies or is rate-limiting). Add a cookies.txt file. Details: ' + msg[:150]
    if 'requested format is not available' in low:
        return 'No downloadable video/audio stream found for this link — it might be an image post rather than a video. Details: ' + msg[:150]
    return 'Invalid link, private/region-locked video, or unsupported site. Details: ' + msg[:150]


def _make_progress_hook(job_id):
    def hook(d):
        job = DOWNLOAD_JOBS.get(job_id)
        if not job:
            return
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate')
            downloaded = d.get('downloaded_bytes', 0)
            if total:
                job['percent'] = round(downloaded / total * 100, 1)
            job['status'] = 'downloading'
        elif d.get('status') == 'finished':
            # ভিডিও ডাউনলোড শেষ, কিন্তু mp3/merge-এর মতো পোস্ট-প্রসেসিং বাকি থাকতে পারে
            job['percent'] = 100
            job['status'] = 'processing'
    return hook


def _cleanup_job_later(job_id, delay=90):
    def _cleanup():
        time.sleep(delay)
        DOWNLOAD_JOBS.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


def _is_permanent_error(msg: str) -> bool:
    """এই ধরনের এরর আবার চেষ্টা করলেও ঠিক হবে না, তাই রিট্রাই করার দরকার নেই।"""
    low = msg.lower()
    permanent_markers = [
        'not supported', 'unsupported url', 'login', 'private', 'sign in',
        'requested format is not available', 'video unavailable', 'removed',
        'copyright', 'does not exist',
    ]
    return any(m in low for m in permanent_markers)


def _run_download_job(job_id, url, q):
    job = DOWNLOAD_JOBS[job_id]
    max_attempts = 2  # প্রথমবার ফেইল করলে একবার অটো রিট্রাই

    for attempt in range(1, max_attempts + 1):
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            unique_prefix = str(int(time.time() * 1000))
            outtmpl = os.path.join(DOWNLOAD_DIR, f"{unique_prefix}_%(title)s.%(ext)s")

            ydl_opts = {
                'outtmpl': outtmpl,
                'quiet': True,
                'no_warnings': True,
                'socket_timeout': 20,
                'retries': 2,
                'restrictfilenames': True,
                'merge_output_format': 'mp4',
                'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'web']}},
                'progress_hooks': [_make_progress_hook(job_id)],
            }
            if os.path.exists(COOKIES_FILE):
                ydl_opts['cookiefile'] = COOKIES_FILE

            if q == 'mp3':
                ydl_opts.update({
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '192',
                    }]
                })
            elif q.isdigit():
                ydl_opts['format'] = f'bestvideo[height<={q}]+bestaudio/best[height<={q}]/best'
            else:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                file_path = ydl.prepare_filename(info)
                if q == 'mp3':
                    file_path = os.path.splitext(file_path)[0] + ".mp3"

            if not os.path.exists(file_path):
                raise RuntimeError('Download failed: output file missing')

            job['file_path'] = file_path
            job['filename'] = sanitize_filename(os.path.basename(file_path))
            job['percent'] = 100
            job['status'] = 'finished'
            record_download(url)
            return

        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            logger.error("download DownloadError (attempt %d) for %s: %s", attempt, url, e)
            if attempt < max_attempts and not _is_permanent_error(msg):
                job['status'] = 'retrying'
                time.sleep(2)
                continue
            job['status'] = 'error'
            job['error'] = _map_download_error(e)
            record_error('download', url, msg[:200])
            _cleanup_job_later(job_id)
            return
        except Exception as e:
            logger.exception("download unexpected error (attempt %d) for %s", attempt, url)
            if attempt < max_attempts:
                job['status'] = 'retrying'
                time.sleep(2)
                continue
            job['status'] = 'error'
            job['error'] = f'Server error: {e}'
            record_error('download', url, str(e)[:200])
            _cleanup_job_later(job_id)
            return


@app.route('/start_download')
def start_download():
    url = request.args.get('url', '').strip()
    q = request.args.get('q', 'best').strip()

    if not url:
        return jsonify({'error': 'No URL provided'}), 400
    if is_youtube_url(url):
        return jsonify({'error': 'YouTube is not supported on this site. Please use Facebook, TikTok, Instagram, or Pinterest links.'}), 400

    job_id = uuid.uuid4().hex
    DOWNLOAD_JOBS[job_id] = {
        'status': 'starting',
        'percent': 0,
        'file_path': None,
        'filename': None,
        'error': None,
    }
    thread = threading.Thread(target=_run_download_job, args=(job_id, url, q), daemon=True)
    thread.start()
    return jsonify({'job_id': job_id})


@app.route('/progress/<job_id>')
def progress(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found or expired'}), 404
    return jsonify({
        'status': job['status'],
        'percent': job['percent'],
        'error': job['error'],
    })


@app.route('/fetch_file/<job_id>')
def fetch_file(job_id):
    job = DOWNLOAD_JOBS.get(job_id)
    if not job or job['status'] != 'finished' or not job['file_path']:
        return jsonify({'error': 'File not ready'}), 400

    file_path = job['file_path']
    filename = job['filename']

    @after_this_request
    def cleanup(response):
        def _delete():
            time.sleep(2)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError:
                pass
            DOWNLOAD_JOBS.pop(job_id, None)
        threading.Thread(target=_delete, daemon=True).start()
        return response

    return send_file(file_path, as_attachment=True, download_name=filename)


@app.route('/admin/<password>')
def admin_panel(password):
    if password != ADMIN_PASSWORD:
        return jsonify({'error': 'Not found'}), 404

    data = load_analytics()
    rows = "".join(
        f"<tr><td>{e['time']}</td><td>{e['source']}</td><td style='max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'>{e['url']}</td><td>{e['message']}</td></tr>"
        for e in data['recent_errors']
    ) or "<tr><td colspan='4' style='text-align:center;color:#888;'>কোনো এরর নেই 🎉</td></tr>"

    platform_rows = "".join(
        f"<tr><td>{p.title()}</td><td>{c}</td></tr>"
        for p, c in data['downloads_by_platform'].items()
    )

    html = f"""
    <!DOCTYPE html><html><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin — Omor Downloader</title>
    <style>
        body {{ background:#05060a; color:#f2f2f2; font-family: -apple-system, sans-serif; padding: 16px; }}
        h1 {{ font-size:1.3em; background: linear-gradient(90deg,#00e5ff,#a742ff); -webkit-background-clip:text; background-clip:text; color:transparent; }}
        .cards {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:20px; }}
        .card {{ background:rgba(255,255,255,0.05); border:1px solid rgba(0,229,255,0.18); border-radius:14px; padding:14px 18px; flex:1; min-width:120px; }}
        .card .num {{ font-size:1.6em; font-weight:800; color:#00e5ff; }}
        .card .label {{ font-size:0.75em; color:#9aa3ad; }}
        table {{ width:100%; border-collapse:collapse; margin-bottom:24px; font-size:0.82em; }}
        th, td {{ padding:8px; border-bottom:1px solid rgba(255,255,255,0.08); text-align:left; }}
        th {{ color:#9aa3ad; font-weight:600; }}
        h2 {{ font-size:1em; color:#f2f2f2; margin-top:24px; }}
    </style></head><body>
        <h1>📊 Omor Downloader — Admin</h1>
        <div class="cards">
            <div class="card"><div class="num">{data['visits']}</div><div class="label">Total Visits</div></div>
            <div class="card"><div class="num">{data['total_downloads']}</div><div class="label">Total Downloads</div></div>
            <div class="card"><div class="num">{len(data['recent_errors'])}</div><div class="label">Recent Errors</div></div>
        </div>

        <h2>Downloads by Platform</h2>
        <table><tr><th>Platform</th><th>Count</th></tr>{platform_rows}</table>

        <h2>Recent Errors (সর্বশেষ ৩০টা)</h2>
        <table><tr><th>Time</th><th>Source</th><th>URL</th><th>Message</th></tr>{rows}</table>
    </body></html>
    """
    return html


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    # Render একটা এনভায়রনমেন্ট ভ্যারিয়েবল হিসেবে PORT দিয়ে দেয় —
    # স্থানীয়ভাবে টেস্ট করলে (PORT সেট না থাকলে) ৫০০০ ব্যবহার হবে।
    port = int(os.environ.get("PORT", 5000))
    # debug=False Production-এ বাধ্যতামূলক — Werkzeug debugger অন
    # থাকলে যে কেউ সার্ভারে কোড রান করাতে পারবে।
    # threaded=True একসাথে একাধিক ইউজারের রিকোয়েস্ট হ্যান্ডেল করবে।
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
