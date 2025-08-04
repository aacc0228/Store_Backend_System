import hashlib

def generate_md5(password):
    """將傳入的字串轉換為 MD5 雜湊值"""
    md5_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
    return md5_hash

if __name__ == '__main__':
    plain_password = input("請輸入您要加密的密碼: ")
    hashed_password = generate_md5(plain_password)
    print(f"\n您的明文密碼是: {plain_password}")
    print(f"產生的 MD5 雜湊值是: {hashed_password}")
    print("\n請將這個雜湊值複製到您的 credentials.txt 檔案中。")