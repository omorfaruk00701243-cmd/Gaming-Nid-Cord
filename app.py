from flask import Flask, render_template, request

app = Flask(__name__)
# একসাথে খুব বড় (৫ MB এর বেশি) রিকোয়েস্ট এলে রিজেক্ট করে দেয়,
# যাতে বড় ছবির কারণে পুরো প্রসেস ক্র্যাশ করে সার্ভার বন্ধ না হয়ে যায়
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

@app.route('/', methods=['GET', 'POST'])
def index():
    card_data = None
    if request.method == 'POST':
        try:
            card_data = {
                'name': request.form.get('name', 'N/A'),
                'nickname': request.form.get('nickname', 'N/A'),
                'uid': request.form.get('uid', 'N/A'),
                'level': request.form.get('level', '1'),
                'region': request.form.get('region', 'BD'),
                'badge': request.form.get('badge', 'Bronze'),
                'photo': request.form.get('photo_base64', '')
            }
        except Exception as e:
            print(f"[ERROR] ফর্ম প্রসেস করতে সমস্যা হয়েছে: {e}")
            card_data = None
    return render_template('index.html', card_data=card_data)

@app.errorhandler(413)
def too_large(e):
    return "ছবির সাইজ অনেক বড়, দয়া করে ছোট ছবি ব্যবহার করুন।", 413

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
