# user_management.py
import csv
import os
from datetime import datetime

USER_CSV_FILE = "user_data/users.csv"

def create_user_csv():
    """ユーザー管理用CSVファイルを作成"""
    os.makedirs("user_data", exist_ok=True)
    
    if not os.path.exists(USER_CSV_FILE):
        with open(USER_CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "username", "display_name", "created_date", 
                "last_session", "total_sessions", "notes"
            ])
        print(f"[INFO] Created user management CSV: {USER_CSV_FILE}")

def add_user(username, display_name=None, notes=""):
    """ユーザーを追加"""
    create_user_csv()
    
    # 既存ユーザーをチェック
    existing_users = get_users()
    if username in [user['username'] for user in existing_users]:
        print(f"[WARN] User '{username}' already exists")
        return False
    
    # 新しいユーザーを追加
    with open(USER_CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            username,
            display_name or username,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "",  # last_session
            "0",  # total_sessions
            notes
        ])
    
    print(f"[INFO] Added user: {username}")
    return True

def get_users():
    """全ユーザー情報を取得"""
    if not os.path.exists(USER_CSV_FILE):
        return []
    
    users = []
    with open(USER_CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append(row)
    return users

def update_user_session(username, session_file):
    """ユーザーのセッション情報を更新"""
    if not os.path.exists(USER_CSV_FILE):
        return False
    
    # 全ユーザーを読み込み
    users = get_users()
    
    # 指定ユーザーを更新
    for user in users:
        if user['username'] == username:
            user['last_session'] = session_file
            user['total_sessions'] = str(int(user['total_sessions']) + 1)
            break
    
    # CSVファイルを再書き込み
    with open(USER_CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "username", "display_name", "created_date", 
            "last_session", "total_sessions", "notes"
        ])
        for user in users:
            writer.writerow([
                user['username'], user['display_name'], user['created_date'],
                user['last_session'], user['total_sessions'], user['notes']
            ])
    
    return True

def list_users():
    """ユーザー一覧を表示"""
    users = get_users()
    if not users:
        print("No users registered")
        return
    
    print("\nRegistered Users:")
    print("-" * 80)
    print(f"{'Username':<15} {'Display Name':<15} {'Created':<20} {'Sessions':<10} {'Notes'}")
    print("-" * 80)
    
    for user in users:
        print(f"{user['username']:<15} {user['display_name']:<15} {user['created_date']:<20} {user['total_sessions']:<10} {user['notes']}")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python user_management.py add <username> [display_name] [notes]")
        print("  python user_management.py list")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "add":
        if len(sys.argv) < 3:
            print("Error: username required")
            sys.exit(1)
        
        username = sys.argv[2]
        display_name = sys.argv[3] if len(sys.argv) > 3 else None
        notes = sys.argv[4] if len(sys.argv) > 4 else ""
        
        add_user(username, display_name, notes)
    
    elif command == "list":
        list_users()
    
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
