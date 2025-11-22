import sys
import os
import webbrowser
from threading import Timer
from flask import Flask, render_template, request, jsonify, send_file
import sqlite3
import datetime
from datetime import timedelta
import pandas as pd
from io import BytesIO

# ==========================================
# 1. Flask App 初始化 (兼容 PyInstaller 打包)
# ==========================================
if getattr(sys, 'frozen', False):
    # 如果是打包后的 exe 运行，去临时目录找 templates
    template_folder = os.path.join(sys._MEIPASS, 'templates')
    app = Flask(__name__, template_folder=template_folder)
else:
    # 正常开发运行
    app = Flask(__name__)

DB_NAME = "vocab_web.db"

# ==========================================
# 2. 数据库相关函数
# ==========================================
def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    # 表1: 单词主表 (增加 notes 字段，笔记现在属于单词本身)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT UNIQUE NOT NULL,
            notes TEXT,
            review_count INTEGER DEFAULT 1,
            created_at TEXT,
            last_reviewed TEXT
        )
    ''')
    # 表2: 释义表 (去掉了 notes 字段)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS meanings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER,
            pos TEXT,
            definition TEXT,
            FOREIGN KEY (word_id) REFERENCES words (id)
        )
    ''')
    conn.commit()
    conn.close()

# 初始化数据库
init_db()

def open_browser():
    webbrowser.open_new('http://127.0.0.1:5000/')

# ==========================================
# 3. 路由逻辑 API
# ==========================================

@app.route('/')
def index():
    conn = get_db_connection()
    
    # 1. 获取统计数据
    today_count = conn.execute("SELECT COUNT(*) FROM words WHERE date(last_reviewed) = date('now', 'localtime')").fetchone()[0]
    total_count = conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    
    # 2. 获取最近记录列表
    # 这里使用 GROUP_CONCAT 把所有释义拼接到一行显示
    # w.notes 直接从 words 表读取
    recent_words = conn.execute('''
        SELECT w.id, w.text, w.notes, w.review_count, w.last_reviewed, 
               GROUP_CONCAT('<span class="badge bg-light text-dark border">' || m.pos || '</span> ' || m.definition, '<br>') as full_def
        FROM words w
        LEFT JOIN meanings m ON w.id = m.word_id
        GROUP BY w.id
        ORDER BY w.last_reviewed DESC LIMIT 30
    ''').fetchall()
    
    conn.close()
    return render_template('index.html', today_count=today_count, total_count=total_count, recent_words=recent_words)

@app.route('/api/check_word', methods=['POST'])
def check_word():
    """检查单词是否存在，返回详情"""
    data = request.json
    text = data.get('word', '').strip().lower()
    conn = get_db_connection()
    word_row = conn.execute("SELECT * FROM words WHERE text = ?", (text,)).fetchone()
    
    if word_row:
        meanings = conn.execute("SELECT * FROM meanings WHERE word_id = ?", (word_row['id'],)).fetchall()
        return jsonify({
            'exists': True,
            'word_id': word_row['id'],
            'notes': word_row['notes'],
            'review_count': word_row['review_count'],
            'meanings': [dict(m) for m in meanings]
        })
    else:
        return jsonify({'exists': False})

@app.route('/api/save_word', methods=['POST'])
def save_word():
    """保存单词（新增/打卡/追加笔记/追加释义）"""
    data = request.json
    text = data.get('word', '').strip().lower()
    pos = data.get('pos')
    definition = data.get('definition')
    new_note = data.get('notes', '').strip()
    force_review = data.get('force_review', False) # 是否是纯打卡模式
    
    now_dt = datetime.datetime.now()
    now_str = now_dt.isoformat()
    
    conn = get_db_connection()
    
    try:
        # 查重
        word_row = conn.execute("SELECT id, review_count, last_reviewed, notes FROM words WHERE text = ?", (text,)).fetchone()
        
        if word_row:
            # --- 旧词逻辑 ---
            word_id = word_row['id']
            last_reviewed_str = word_row['last_reviewed']
            current_count = word_row['review_count']
            old_notes = word_row['notes'] if word_row['notes'] else ""
            
            # 1. 计算冷却时间 (5分钟内不加次数，除非强制打卡)
            try:
                last_time = datetime.datetime.fromisoformat(last_reviewed_str)
            except:
                last_time = now_dt - timedelta(days=1)
            
            if (now_dt - last_time) < timedelta(minutes=5) and not force_review:
                new_count = current_count
            else:
                new_count = current_count + 1
            
            # 2. 笔记追加逻辑
            final_note = old_notes
            if new_note and new_note not in old_notes: 
                if old_notes:
                    final_note = old_notes + "\n" + new_note
                else:
                    final_note = new_note

            conn.execute("UPDATE words SET review_count = ?, last_reviewed = ?, notes = ? WHERE id = ?", 
                         (new_count, now_str, final_note, word_id))
        else:
            # --- 新词逻辑 ---
            cur = conn.execute("INSERT INTO words (text, notes, review_count, created_at, last_reviewed) VALUES (?, ?, 1, ?, ?)", 
                               (text, new_note, now_str, now_str))
            word_id = cur.lastrowid
            
        # 3. 插入新释义 (如果填了的话)
        if definition:
            conn.execute("INSERT INTO meanings (word_id, pos, definition) VALUES (?, ?, ?)",
                         (word_id, pos, definition))
        
        conn.commit()
        return jsonify({'status': 'success'})
    except Exception as e:
        print(f"Save Error: {e}")
        return jsonify({'status': 'error', 'msg': str(e)})
    finally:
        conn.close()

@app.route('/export')
def export_data():
    """导出 Excel"""
    conn = get_db_connection()
    query = '''
        SELECT 
            w.text AS [单词], 
            w.notes AS [笔记],
            m.pos AS [词性], 
            m.definition AS [释义], 
            w.review_count AS [次数], 
            w.last_reviewed AS [时间]
        FROM words w
        LEFT JOIN meanings m ON w.id = m.word_id
        ORDER BY w.last_reviewed DESC
    '''
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='单词记录')
    output.seek(0)
    return send_file(output, download_name=f"Vocab_{datetime.date.today()}.xlsx", as_attachment=True)

# ==========================================
# 4. 管理与编辑 API (新功能)
# ==========================================

@app.route('/api/get_word_details', methods=['POST'])
def get_word_details():
    """获取单词详情（用于管理弹窗）"""
    word_id = request.json.get('id')
    conn = get_db_connection()
    word = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    meanings = conn.execute("SELECT * FROM meanings WHERE word_id = ?", (word_id,)).fetchall()
    conn.close()
    return jsonify({
        'id': word['id'],
        'text': word['text'],
        'meanings': [dict(m) for m in meanings]
    })

@app.route('/api/update_word_text', methods=['POST'])
def update_word_text():
    """修改单词拼写"""
    data = request.json
    word_id = data.get('id')
    new_text = data.get('text').strip().lower()
    conn = get_db_connection()
    try:
        conn.execute("UPDATE words SET text = ? WHERE id = ?", (new_text, word_id))
        conn.commit()
        return jsonify({'status': 'success'})
    except sqlite3.IntegrityError:
        return jsonify({'status': 'error', 'msg': '单词已存在，无法重名！'})
    finally:
        conn.close()

@app.route('/api/update_word_notes', methods=['POST'])
def update_word_notes():
    """修改单词笔记"""
    data = request.json
    conn = get_db_connection()
    conn.execute("UPDATE words SET notes = ? WHERE id = ?", (data['notes'], data['id']))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/update_meaning', methods=['POST'])
def update_meaning():
    """修改某条释义"""
    data = request.json
    conn = get_db_connection()
    conn.execute("UPDATE meanings SET pos=?, definition=? WHERE id=?", 
                 (data['pos'], data['definition'], data['id']))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

@app.route('/api/delete_meaning', methods=['POST'])
def delete_meaning():
    """删除某条释义"""
    conn = get_db_connection()
    conn.execute("DELETE FROM meanings WHERE id = ?", (request.json.get('id'),))
    conn.commit()
    conn.close()
    return jsonify({'status': 'success'})

# ==========================================
# 5. 程序启动入口
# ==========================================
if __name__ == '__main__':
    print("正在启动程序...")
    try:
        # 防止在 debug 模式下的重载器中重复打开浏览器
        if not os.environ.get("WERKZEUG_RUN_MAIN"):
            Timer(1.5, open_browser).start()
        
        # 开发模式用 debug=True，打包 EXE 时请改为 debug=False
        app.run(port=5000, debug=True)
        
    except Exception as e:
        print(f"启动报错: {e}")
        input("按回车键退出...")