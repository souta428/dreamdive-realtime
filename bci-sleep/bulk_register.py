# bulk_register.py
import csv
import os
from user_management import add_user

def bulk_register_from_csv(csv_file):
    """CSVファイルから一括でユーザーを登録"""
    if not os.path.exists(csv_file):
        print(f"[ERROR] CSV file not found: {csv_file}")
        return
    
    print(f"[INFO] Reading users from: {csv_file}")
    
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            username = row['username'].strip()
            if username:
                # display_nameを自動生成 (Member 001形式)
                try:
                    member_num = username.split('_')[1]
                    display_name = f"Member {member_num}"
                except:
                    display_name = username
                
                # ユーザーを登録
                success = add_user(username, display_name, "")
                if success:
                    print(f"[SUCCESS] Registered: {username} -> {display_name}")
                else:
                    print(f"[SKIP] Already exists: {username}")
    
    print("[INFO] Bulk registration completed!")

if __name__ == "__main__":
    import sys
    
    csv_file = "members_to_register.csv"
    if len(sys.argv) > 1:
        csv_file = sys.argv[1]
    
    bulk_register_from_csv(csv_file)
