# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║   法律问答助手 v5.3 — 全量法律库 + AI + 案例搜索     ║
║                                                      ║
║  内置法律库：4868部法律法规 + 司法解释 + 典型案例     ║
║  AI 引擎：  DeepSeek 大模型智能回答                   ║
║  特色功能：联网搜索 · 两高典型案例检索                ║
║  用户系统：用户名密码登录 · 注册 · 记住我             ║
║  性能优化：倒排索引 · 并行搜索 · 联网熔断器           ║
║  部署平台：腾讯云轻量服务器 + PythonAnywhere          ║
╚══════════════════════════════════════════════════════╝
"""

import os
import re
import json
import gzip
import glob
import time
import sqlite3
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import requests as http_requests
from flask import Flask, request, jsonify, render_template_string, session, redirect, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import jieba
    jieba.initialize()  # 启动时预加载词典，避免首次查询卡顿
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'jieba'])
    import jieba
    jieba.initialize()

WEB_SEARCH_AVAILABLE = False
DDGS = None
try:
    from ddgs import DDGS
    WEB_SEARCH_AVAILABLE = True
except ImportError:
    try:
        from duckduckgo_search import DDGS
        WEB_SEARCH_AVAILABLE = True
    except ImportError:
        WEB_SEARCH_AVAILABLE = False

# ---- 联网搜索熔断器 ----
# 部分服务器（如国内机房）无法访问DuckDuckGo，请求会一直卡住。
# 连续失败2次后暂停联网搜索10分钟，期间直接返回空结果，保证搜索秒开。
_web_lock = threading.Lock()
_web_fail_streak = 0
_web_disabled_until = 0.0


def _web_allowed():
    """联网搜索是否可用（已安装且未被熔断）"""
    return WEB_SEARCH_AVAILABLE and time.time() >= _web_disabled_until


def _web_report(ok):
    """汇报一次联网搜索成功/失败，连续失败2次则熔断10分钟"""
    global _web_fail_streak, _web_disabled_until
    with _web_lock:
        if ok:
            _web_fail_streak = 0
        else:
            _web_fail_streak += 1
            if _web_fail_streak >= 2:
                _web_disabled_until = time.time() + 600
                print(f'[联网搜索] 连续失败{_web_fail_streak}次，暂停联网搜索10分钟')


def _ddgs_client():
    """创建带6秒硬超时的DDGS客户端（旧版本不支持timeout参数时自动降级）"""
    try:
        return DDGS(timeout=6)
    except TypeError:
        return DDGS()

# ==================== 配置 ====================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-v4-flash'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAWS_DATA_FILE = os.path.join(BASE_DIR, 'laws_data.json.gz')

# 用户数据库
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'users.db'))
REMEMBER_DAYS = 30  # "记住我"会话时长

# 开发者账户（仅此账户可上传法律文件）
DEV_USERNAME = 'fyql'
DEV_PASSWORD = 'Zbl20080212'

# 联网搜索可信法律域名白名单（用于筛选，过滤杂乱信息）
TRUSTED_LEGAL_DOMAINS = [
    'court.gov.cn',      # 最高人民法院
    'spp.gov.cn',        # 最高人民检察院
    'npc.gov.cn',        # 全国人大
    'gov.cn',            # 各级政府（含地方法规）
    'pkulaw.com',        # 北大法宝
    'chinalaw.gov.cn',   # 司法部
    'rmfyalk.court.gov.cn',  # 人民法院案例库
    'wenshu.court.gov.cn',   # 中国裁判文书网
    '12309.gov.cn',      # 中国检察网
    'ts.12348.gov.cn',   # 中国法律服务网
    'flk.npc.gov.cn',    # 国家法律法规数据库
    'gongbao.court.gov.cn',  # 最高人民法院公报
    'ipc.court.gov.cn',  # 知识产权法庭
    'moj.gov.cn',        # 司法部
    'samr.gov.cn',       # 市场监管总局
    'mohrss.gov.cn',     # 人社部
    'chinatax.gov.cn',   # 税务总局
    'pbc.gov.cn',        # 人民银行
    'csrc.gov.cn',       # 证监会
    'sccourt.gov.cn',    # 四川法院
    'bjcourt.gov.cn',    # 北京法院
    'shcourt.gov.cn',    # 上海法院
    'gccourt.gov.cn',    # 广东法院
]

# 低质量网站黑名单（范文/模板/内容农场/自媒体，内容不可靠，直接过滤）
JUNK_DOMAINS = [
    'yjbys.com', 'ruiwen.com', 'pincai.com', 'oh100.com', 'unjs.com',
    'wenku.baidu.com', 'zhidao.baidu.com', 'baijiahao.baidu.com',
    'jianshu.com', 'sohu.com', '163.com', 'toutiao.com', 'qq.com',
    'docin.com', 'doc88.com', 'book118.com', 'csdn.net', 'cnrencai.com',
    'yuedu.baidu.com', 'max.book118.com',
]
LAWS_DIR = os.path.join(BASE_DIR, 'laws')


# ==================== 用户数据库 ====================

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT,
            login_count INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_user ON search_history(user_id, id);
    ''')
    conn.commit()
    conn.close()


# ==================== 用户名密码验证 ====================

def validate_username(username):
    """用户名：最多6个汉字或12个英文字母/数字（汉字算2个单位，总长不超过12）"""
    if not username:
        return False, '请输入用户名'
    if not re.match(r'^[\u4e00-\u9fff\w]+$', username):
        return False, '用户名只能包含汉字、字母和数字'
    length = sum(2 if '\u4e00' <= ch <= '\u9fff' else 1 for ch in username)
    if length > 12:
        return False, '用户名太长（最多6个汉字或12个英文字符）'
    if length < 2:
        return False, '用户名太短（至少2个字符）'
    return True, ''


def validate_password(password):
    """密码：最多30字符，必须包含大写字母、小写字母和数字"""
    if not password:
        return False, '请输入密码'
    if len(password) > 30:
        return False, '密码不能超过30个字符'
    if len(password) < 6:
        return False, '密码至少需要6个字符'
    if not re.search(r'[A-Z]', password):
        return False, '密码必须包含至少一个大写字母'
    if not re.search(r'[a-z]', password):
        return False, '密码必须包含至少一个小写字母'
    if not re.search(r'[0-9]', password):
        return False, '密码必须包含至少一个数字'
    return True, ''


def get_or_create_user(username, password_hash=None):
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if user:
        if password_hash is not None:
            return None  # 注册时用户名已存在
        db.execute('UPDATE users SET last_login=?, login_count=login_count+1 WHERE id=?', (now, user['id']))
        db.commit()
        return dict(user)
    else:
        if password_hash is None:
            return None  # 登录时用户不存在
        cur = db.execute('INSERT INTO users (username, password_hash, created_at, last_login, login_count) VALUES (?,?,?,?,1)',
                         (username, password_hash, now, now))
        db.commit()
        return {'id': cur.lastrowid, 'username': username, 'created_at': now, 'last_login': now, 'login_count': 1}


# ==================== 登录装饰器 ====================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({'error': '请先登录', 'need_login': True}), 401
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


# ==================== 登录/注册路由 ====================

@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/')
    return render_template_string(LOGIN_TEMPLATE)


@app.route('/register')
def register_page():
    if 'user_id' in session:
        return redirect('/')
    return render_template_string(REGISTER_TEMPLATE)


@app.route('/api/register', methods=['POST'])
def api_register():
    data = request.get_json(silent=True)
    if not data:
        return jsonify(success=False, message='请求格式错误'), 400
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    ok, msg = validate_username(username)
    if not ok:
        return jsonify(success=False, message=msg), 400
    ok, msg = validate_password(password)
    if not ok:
        return jsonify(success=False, message=msg), 400
    pw_hash = generate_password_hash(password)
    user = get_or_create_user(username, pw_hash)
    if user is None:
        return jsonify(success=False, message='用户名已被注册，请换一个'), 400
    # 注册后自动登录
    session.permanent = True
    app.permanent_session_lifetime = timedelta(days=7)
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['login_time'] = datetime.now().isoformat()
    return jsonify(success=True, message='注册成功', redirect='/')


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json(silent=True)
    if not data:
        return jsonify(success=False, message='请求格式错误'), 400
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    remember = bool(data.get('remember', False))
    if not username or not password:
        return jsonify(success=False, message='请输入用户名和密码'), 400
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not user:
        return jsonify(success=False, message='用户名或密码错误'), 400
    if not check_password_hash(user['password_hash'], password):
        return jsonify(success=False, message='用户名或密码错误'), 400
    # 更新登录信息
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('UPDATE users SET last_login=?, login_count=login_count+1 WHERE id=?', (now, user['id']))
    db.commit()
    # 创建会话
    session.permanent = True
    if remember:
        app.permanent_session_lifetime = timedelta(days=REMEMBER_DAYS)
    else:
        app.permanent_session_lifetime = timedelta(days=7)
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['login_time'] = datetime.now().isoformat()
    return jsonify(success=True, message='登录成功', redirect='/')


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify(success=True, message='已退出')


@app.route('/api/user-info')
@login_required
def api_user_info():
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()
    if not user:
        session.clear()
        return jsonify(error='用户不存在'), 404
    return jsonify(id=user['id'], username=user['username'],
                   created_at=user['created_at'], login_count=user['login_count'])


# ==================== 联网搜索 ====================

def web_search(question, max_results=6):
    """联网搜索法律信息，经过域名白名单和相关性筛选，过滤杂乱信息"""
    if not _web_allowed():
        return []
    results = []
    try:
        query = f'{question} 法律 法规 司法解释'
        try:
            raw = list(_ddgs_client().text(query, region='cn-zh', max_results=15))
        except TypeError:
            raw = list(_ddgs_client().text(query, max_results=15))
        for item in raw:
            url = item.get('href', '') or item.get('url', '') or item.get('link', '')
            title = (item.get('title', '') or '').strip()
            body = (item.get('body', '') or item.get('snippet', '') or item.get('content', '') or '').strip()
            if not title or not body:
                continue
            if any(j in url for j in JUNK_DOMAINS):
                continue
            is_trusted = any(d in url for d in TRUSTED_LEGAL_DOMAINS)
            legal_kws = ['法', '条例', '规定', '解释', '判决', '案例', '法院', '检察', '条款', '规章']
            is_relevant = any(k in title or k in body for k in legal_kws)
            if not (is_trusted or is_relevant):
                continue
            has_cite = bool(re.search(r'《[^》]{2,}》|第[一二三四五六七八九十百零\d]+条', title + body))
            results.append({'title': title, 'body': body[:400], 'url': url, 'trusted': is_trusted, '_cite': has_cite})
        results.sort(key=lambda x: (not x['trusted'], not x['_cite']))
        for r in results:
            r.pop('_cite', None)
        results = results[:max_results]
    except Exception as e:
        print(f'[联网搜索] 失败: {e}')
        return []
    return results


def official_web_search(question, max_results=6):
    """严格官方搜索：仅接受政府/官方来源，排除一切私人网站和个人观点（并行加速）"""
    if not _web_allowed():
        return []
    results = []
    try:
        queries = [
            f'{question} 法律条文 法规 司法解释',
            f'{question} 地方法规 条例 规定',
        ]

        def _do_query(query):
            try:
                raw = list(_ddgs_client().text(query, region='cn-zh', max_results=8))
            except TypeError:
                raw = list(_ddgs_client().text(query, max_results=8))
            return raw

        # 并行执行搜索（不等待卡死的线程，最多等8秒）
        all_raw = []
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            futures = {executor.submit(_do_query, q): q for q in queries}
            for future in as_completed(futures, timeout=8):
                try:
                    all_raw.extend(future.result())
                except Exception:
                    pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        seen_urls = set()
        opinion_markers = ['博客', '论坛', '知乎', '百度知道', '个人', '我认为', '我觉得',
                           '网友', '楼主', '自媒体', '公众号', '头条号', '百家号']
        for item in all_raw:
            url = item.get('href', '') or item.get('url', '') or item.get('link', '')
            title = (item.get('title', '') or '').strip()
            body = (item.get('body', '') or item.get('snippet', '') or item.get('content', '') or '').strip()
            if not title or not body:
                continue
            if url in seen_urls:
                continue
            if not any(d in url for d in TRUSTED_LEGAL_DOMAINS):
                continue
            if any(j in url for j in JUNK_DOMAINS):
                continue
            if any(m in title or m in body for m in opinion_markers):
                continue
            seen_urls.add(url)
            results.append({'title': title, 'body': body[:500], 'url': url, 'trusted': True})
            if len(results) >= max_results:
                break
        results = results[:max_results]
    except Exception as e:
        print(f'[官方搜索] 失败: {e}')
        return []
    return results


def case_search(question, max_results=8):
    """专门搜索两高典型案例，优先从官方案例库和权威来源获取"""
    if not _web_allowed():
        return []
    results = []
    try:
        query = f'{question} 典型案例 最高人民法院 最高人民检察院'
        try:
            raw = list(_ddgs_client().text(query, region='cn-zh', max_results=20))
        except TypeError:
            raw = list(_ddgs_client().text(query, max_results=20))
        case_domains = ['court.gov.cn', 'spp.gov.cn', 'rmfyalk.court.gov.cn', 'wenshu.court.gov.cn', 'pkulaw.com', 'chinalawinfo.com']
        case_kws = ['典型案例', '指导性案例', '案例', '判决', '裁定', '公诉', '审判']
        for item in raw:
            url = item.get('href', '') or item.get('url', '') or item.get('link', '')
            title = (item.get('title', '') or '').strip()
            body = (item.get('body', '') or item.get('snippet', '') or item.get('content', '') or '').strip()
            if not title or not body:
                continue
            if any(j in url for j in JUNK_DOMAINS):
                continue
            is_case_source = any(d in url for d in case_domains)
            is_case_related = any(k in title or k in body for k in case_kws)
            if not (is_case_source or is_case_related):
                continue
            results.append({'title': title, 'body': body[:500], 'url': url, 'trusted': is_case_source})
        results.sort(key=lambda x: not x['trusted'])
        results = results[:max_results]
    except Exception as e:
        print(f'[案例搜索] 失败: {e}')
        return []
    return results


# ==================== 文档管理器 ====================
class LegalDocManager:
    def __init__(self):
        self.documents = {}
        self.chunks = []
        self.law_count = 0
        self.category_count = 0
        self._inverted_index = defaultdict(list)  # keyword -> [(chunk_idx, count)]
        self._index_ready = False

    def load_builtin_laws(self):
        """启动时加载全量法律数据"""
        categories = set()
        if os.path.isfile(LAWS_DATA_FILE):
            try:
                with gzip.open(LAWS_DATA_FILE, 'rt', encoding='utf-8') as f:
                    laws = json.load(f)
                for law in laws:
                    title = law.get('title', '')
                    category = law.get('category', '其他')
                    content = law.get('content', '')
                    if not title or not content.strip():
                        continue
                    key = f'{category}/{title}'
                    self.documents[key] = content
                    categories.add(category)
                self.law_count = len(self.documents)
                self.category_count = len(categories)
                print(f'[法律库] 已加载 {self.law_count} 部法律, {self.category_count} 个分类')
            except Exception as e:
                print(f'[法律库] 加载压缩数据失败: {e}')
        if os.path.isdir(LAWS_DIR):
            for filepath in glob.glob(os.path.join(LAWS_DIR, '*.txt')):
                name = os.path.basename(filepath)
                if name in self.documents:
                    continue
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    if content.strip():
                        self.documents[name] = content
                except Exception:
                    pass
        self._rebuild_chunks()
        print(f'[法律库] 共 {len(self.documents)} 部文档, {len(self.chunks)} 个条文/段落')

    def add_document(self, filename, content):
        self.documents[filename] = content
        self._rebuild_chunks()

    def remove_document(self, filename):
        self.documents.pop(filename, None)
        self._rebuild_chunks()

    def get_doc_list(self):
        result = []
        for name in self.documents:
            art = sum(1 for c in self.chunks if c['source'] == name and c.get('is_article'))
            total = sum(1 for c in self.chunks if c['source'] == name)
            builtin = not name.startswith('upload_')
            result.append({'name': name, 'articles': art, 'chunks': total, 'builtin': builtin})
        return result

    def get_stats(self):
        cats = {}
        for name in self.documents:
            if name.startswith('upload_'):
                continue
            cat = name.split('/')[0] if '/' in name else '其他'
            cats[cat] = cats.get(cat, 0) + 1
        return {'total': self.law_count, 'categories': cats, 'chunks': len(self.chunks)}

    def _rebuild_chunks(self):
        self.chunks = []
        for filename, content in self.documents.items():
            self.chunks.extend(self._split_content(content, filename))
        self._build_index()

    def _build_index(self):
        """构建倒排索引：keyword -> [(chunk_idx, count)]，搜索时直接查索引而非遍历全部"""
        self._inverted_index = defaultdict(list)
        for idx, chunk in enumerate(self.chunks):
            combined = chunk['content'] + ' ' + chunk['title']
            words = jieba.lcut(combined)
            word_count = defaultdict(int)
            for w in words:
                w = w.strip()
                if len(w) >= 2 and w not in self._STOP and not re.match(r'^[^\u4e00-\u9fff\w]+$', w):
                    word_count[w] += 1
            for w, cnt in word_count.items():
                self._inverted_index[w].append((idx, cnt))
        self._index_ready = True
        print(f'[索引] 倒排索引构建完成: {len(self._inverted_index)} 个词条, 覆盖 {len(self.chunks)} 个段落')

    def _split_content(self, content, filename):
        chunks = []
        pattern = r'第([零一二三四五六七八九十百千\d]+)条'
        matches = list(re.finditer(pattern, content))
        if len(matches) >= 3:
            if matches[0].start() > 0:
                header = content[:matches[0].start()].strip()
                if header and len(header) > 10:
                    chunks.append(self._mk('前言/目录', header[:2000], filename, False, ''))
            for i, match in enumerate(matches):
                num = match.group(1)
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                text = content[start:end].strip()
                if text and len(text) > 5:
                    chunks.append(self._mk(f'第{num}条', text, filename, True, num))
        else:
            paragraphs = re.split(r'\n\s*\n', content)
            idx = 0
            for p in paragraphs:
                p = p.strip()
                if not p or len(p) < 5:
                    continue
                idx += 1
                if len(p) > 600:
                    for j in range(0, len(p), 500):
                        seg = p[j:j + 500]
                        if len(seg) > 10:
                            chunks.append(self._mk(f'段落{idx}', seg, filename, False, ''))
                else:
                    chunks.append(self._mk(f'段落{idx}', p, filename, False, ''))
        return chunks

    @staticmethod
    def _mk(title, content, source, is_article, article_num):
        return {'title': title, 'content': content, 'source': source,
                'is_article': is_article, 'article_num': article_num}

    @staticmethod
    def _cn2int(cn):
        if cn.isdigit():
            return int(cn)
        d = {'零':0,'一':1,'二':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10,'百':100,'千':1000}
        result, cur = 0, 0
        for ch in cn:
            v = d.get(ch)
            if v is None:
                continue
            if v >= 10:
                result += (cur or 1) * v
                cur = 0
            else:
                cur = v
        return result + cur

    _STOP = {
        '的','了','是','在','我','有','和','就','不','人','都','一','一个','上','也','很','到',
        '说','要','去','你','会','着','没有','看','好','自己','这','他','她','吗','呢','什么',
        '怎么','如何','请问','请','哪些','哪','那','这个','那个','可以','能','啊','吧','把','被',
        '给','让','从','对','向','与','及','等','中','下','时','为','以','之','所','其','而',
        '但','又','或','如果','因为','所以','但是','而且',
    }

    def _keywords(self, text):
        words = jieba.lcut(text)
        seen, result = set(), []
        for w in words:
            w = w.strip()
            if len(w) >= 2 and w not in self._STOP and w not in seen and not re.match(r'^[^\u4e00-\u9fff\w]+$', w):
                seen.add(w)
                result.append(w)
        return result

    def search(self, question, top_n=10):
        kws = self._keywords(question)
        if not kws:
            return []
        asked_nums = set()
        for m in re.finditer(r'第([零一二三四五六七八九十百千\d]+)条', question):
            asked_nums.add(self._cn2int(m.group(1)))
        law_names = set()
        for m in re.finditer(r'《(.+?)》', question):
            law_names.add(m.group(1))

        # 使用倒排索引快速定位相关段落
        if self._index_ready:
            scores = defaultdict(float)
            for kw in kws:
                weight = 1.5 if len(kw) >= 3 else 1.0
                postings = self._inverted_index.get(kw)
                if postings:
                    for idx, cnt in postings:
                        scores[idx] += cnt * weight
            # 条文号精确匹配加分
            if asked_nums:
                for idx in list(scores.keys()):
                    chunk = self.chunks[idx]
                    if chunk['is_article']:
                        cn = self._cn2int(chunk['article_num'])
                        if cn in asked_nums:
                            scores[idx] += 50
            # 法律名称匹配加分
            if law_names:
                for idx in list(scores.keys()):
                    src = self.chunks[idx]['source']
                    for ln in law_names:
                        if ln in src:
                            scores[idx] += 30
                            break
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            return [self.chunks[idx] for idx, _ in ranked[:top_n]]
        else:
            # 索引未就绪时的回退方案
            scored = []
            for chunk in self.chunks:
                score = 0.0
                combined = chunk['content'] + ' ' + chunk['title']
                for kw in kws:
                    cnt = combined.count(kw)
                    if cnt:
                        score += cnt * (1.5 if len(kw) >= 3 else 1.0)
                if score > 0:
                    scored.append((score, chunk))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [c for _, c in scored[:top_n]]

    def search_only(self, question):
        # 本地搜索和联网搜索并行执行，联网搜索最多等4秒
        # 用 shutdown(wait=False) 确保不被卡死的联网线程拖住响应
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            local_future = executor.submit(self.search, question, 8)
            web_future = executor.submit(official_web_search, question, 5)
            results = local_future.result()
            try:
                web_results = web_future.result(timeout=4)
                _web_report(True)
            except Exception:
                web_results = []
                _web_report(False)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        if not self.chunks and not web_results:
            return {'answer': '法律库为空，请上传法律文件。', 'citations': [], 'ai_used': False}
        if not results and not web_results:
            return {'answer': '抱歉，没有找到相关法律条文。建议换一种方式提问，或开启联网搜索获取更多结果。',
                    'citations': [], 'ai_used': False}
        kw_str = '、'.join(self._keywords(question)[:6])
        citations = []
        web_citations = []
        display_parts = []
        # 本地法律库结果
        if results:
            header = f"【法律库检索结果】共 {len(results)} 条"
            if kw_str:
                header += f"（关键词：{kw_str}）"
            display_parts.append(header + '：\n')
            for chunk in results:
                src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
                display = chunk['content'][:600] + ('……' if len(chunk['content']) > 600 else '')
                display_parts.append(f"● {chunk['title']}（{src_display}）\n{display}")
                citations.append({'title': chunk['title'], 'source': src_display})
        # 官方联网补充结果
        if web_results:
            display_parts.append(f"\n【官方来源补充】共 {len(web_results)} 条（仅政府/司法机关官方发布）：\n")
            for wr in web_results:
                display_parts.append(f"● {wr['title']}\n  {wr['body'][:300]}")
                web_citations.append({'title': wr['title'][:40], 'source': '官方来源', 'url': wr['url']})
        # 严谨性提示
        display_parts.append('\n⚖️ 以上结果均来自法律法规数据库及政府官方渠道，仅供参考。具体案件的法律适用请以有权机关的正式文本为准，建议咨询专业律师获取针对性意见。')
        return {'answer': '\n\n'.join(display_parts), 'citations': citations, 'web_citations': web_citations, 'ai_used': False}

    def search_cases_only(self, question):
        # 并行执行本地搜索和联网案例搜索（联网最多等4秒，不等待卡死的线程）
        executor = ThreadPoolExecutor(max_workers=2)
        try:
            local_future = executor.submit(self.search, question, 6)
            web_future = executor.submit(case_search, question)
            local_results = local_future.result()
            try:
                web_cases = web_future.result(timeout=4)
                _web_report(True)
            except Exception:
                web_cases = []
                _web_report(False)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        case_results = [c for c in local_results if '案例' in c['source']]
        other_results = [c for c in local_results if '案例' not in c['source']]
        local_case = (case_results + other_results)[:5]
        if not local_case and not web_cases:
            return {'answer': '未找到相关典型案例。建议换一种方式提问，或前往人民法院案例库（rmfyalk.court.gov.cn）检索。',
                    'citations': [], 'web_citations': [], 'ai_used': False}
        citations = []
        web_citations = []
        parts = []
        if local_case:
            parts.append(f"【库内相关内容】共 {len(local_case)} 条：\n")
            for chunk in local_case:
                src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
                parts.append(f"● {chunk['title']}（{src_display}）\n{chunk['content'][:400]}")
                citations.append({'title': chunk['title'], 'source': src_display})
        if web_cases:
            parts.append(f"\n【两高典型案例（联网搜索）】共 {len(web_cases)} 条：\n")
            for wc in web_cases:
                parts.append(f"● {wc['title']} {('[官方来源]' if wc['trusted'] else '')}\n  {wc['body'][:200]}")
                web_citations.append({'title': wc['title'][:35], 'source': '案例搜索', 'url': wc['url']})
        return {'answer': '\n\n'.join(parts), 'citations': citations, 'web_citations': web_citations, 'ai_used': False}

    def answer_with_ai(self, question, history=None, use_web=False, use_case=False):
        # 并行执行本地搜索、联网搜索、案例搜索（联网最多各等4秒，不等待卡死的线程）
        executor = ThreadPoolExecutor(max_workers=3)
        try:
            local_future = executor.submit(self.search, question)
            web_future = executor.submit(web_search, question) if use_web else None
            case_future = executor.submit(case_search, question) if use_case else None
            results = local_future.result()
            web_results = []
            if web_future:
                try:
                    web_results = web_future.result(timeout=4)
                    _web_report(True)
                except Exception:
                    _web_report(False)
            case_results = []
            if case_future:
                try:
                    case_results = case_future.result(timeout=4)
                    _web_report(True)
                except Exception:
                    _web_report(False)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        if not self.chunks and not web_results and not case_results:
            return {'answer': '法律库为空，请上传法律文件。', 'citations': [], 'ai_used': False}
        if not results and not web_results and not case_results:
            if DEEPSEEK_API_KEY:
                ai_answer = self._call_deepseek(question, '', history)
                if ai_answer:
                    return {'answer': ai_answer, 'citations': [], 'ai_used': True}
            return {'answer': '抱歉，没有找到相关法律条文。建议换一种方式提问，或上传更多法律文件。',
                    'citations': [], 'ai_used': False}

        context_parts = []
        citations = []
        for chunk in results:
            src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
            context_parts.append(f"【{chunk['title']}】（{src_display}）\n{chunk['content']}")
            citations.append({'title': chunk['title'], 'source': src_display})

        web_citations = []
        if web_results:
            context_parts.append('=== 以下为联网搜索到的权威法律信息（已筛选） ===')
            for wr in web_results:
                context_parts.append(f"【网络资料】{wr['title']}\n{wr['body']}")
                web_citations.append({'title': wr['title'][:30], 'source': '联网搜索', 'url': wr['url']})
        if case_results:
            context_parts.append('=== 以下为搜索到的两高典型案例（已筛选） ===')
            for cr in case_results:
                context_parts.append(f"【典型案例】{cr['title']}\n{cr['body']}")
                web_citations.append({'title': cr['title'][:35], 'source': '案例搜索', 'url': cr['url']})
        context = '\n\n'.join(context_parts)

        if DEEPSEEK_API_KEY:
            ai_answer = self._call_deepseek(question, context, history, has_web=bool(web_results), case_mode=bool(case_results))
            if ai_answer:
                return {'answer': ai_answer, 'citations': citations, 'web_citations': web_citations, 'ai_used': True}

        kw_str = '、'.join(self._keywords(question)[:6])
        header = f"找到以下 {len(results)} 条相关法条"
        if kw_str:
            header += f"（关键词：{kw_str}）"
        header += '：\n\n'
        display_parts = []
        for chunk in results[:5]:
            src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
            display = chunk['content'][:500] + ('……' if len(chunk['content']) > 500 else '')
            display_parts.append(f"【{chunk['title']}】（{src_display}）\n{display}")
        return {'answer': header + '\n\n'.join(display_parts), 'citations': citations, 'web_citations': web_citations, 'ai_used': False}

    def _call_deepseek(self, question, context, history=None, has_web=False, case_mode=False):
        if case_mode:
            system_prompt = (
                '你是一位专业的中国法律案例分析专家，名叫"法小智"。你精通最高人民法院和最高人民检察院发布的'
                '指导性案例、典型案例，以及各级法院的重要判例。\n\n'
                '回答要求：\n'
                '1. 结合提供的案例资料和联网搜索结果，分析相关典型案例\n'
                '2. 说明案例的裁判要旨、法律适用要点\n'
                '3. 引用具体案例名称和裁判观点\n'
                '4. 结合相关法律条文分析案例的法律依据\n'
                '5. 如果涉及多个案例，归纳共同的法律原则\n'
                '6. 注明案例来源（如"据最高人民法院发布的第X批指导性案例"）\n'
                '7. 回答末尾提醒：具体案件建议咨询专业律师\n'
                '8. 不要编造不存在的案例'
            )
        else:
            system_prompt = (
                '你是一位专业的中国法律顾问，名叫"法小智"。你拥有涵盖宪法、民法典、刑法、行政法、经济法、'
                '社会法、诉讼法、司法解释等4800余部法律法规的完整知识库。请根据提供的法律条文回答用户的问题。\n\n'
                '回答要求：\n'
                '1. 准确引用具体法条（如"根据《民法典》第577条"）\n'
                '2. 用通俗易懂的语言解释法律含义\n'
                '3. 如果涉及多个法条，分点说明\n'
                '4. 如果提供的法条不足以完整回答，请如实说明并给出一般性法律建议\n'
                '5. 回答末尾提醒用户：具体案件建议咨询专业律师\n'
                '6. 不要编造不存在的法条\n'
                '7. 结合之前的对话上下文理解用户的追问，保持回答连贯'
            )
        if has_web:
            system_prompt += (
                '\n8. 本次回答附带了联网搜索到的资料（已初步筛选）。请仔细甄别，只采纳其中权威、准确、'
                '与问题相关的内容，剔除杂乱或不可靠的信息。若网络资料与法律条文冲突，以法律条文为准。'
                '引用网络资料时请注明来源（如"据最高人民法院发布的典型案例"）。'
            )
        if context:
            user_msg = f'以下是相关法律条文：\n\n{context}\n\n---\n用户问题：{question}'
        else:
            user_msg = f'用户问题：{question}\n\n（未找到直接相关的法条，请根据你的法律知识回答，并注明仅供参考）'
        messages = [{'role': 'system', 'content': system_prompt}]
        if history:
            for h in history[-12:]:
                if h.get('role') in ('user', 'assistant') and h.get('content'):
                    messages.append({'role': h['role'], 'content': h['content']})
        messages.append({'role': 'user', 'content': user_msg})
        try:
            resp = http_requests.post(
                DEEPSEEK_URL,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {DEEPSEEK_API_KEY}'},
                json={'model': DEEPSEEK_MODEL, 'messages': messages, 'temperature': 0.3, 'max_tokens': 2000},
                timeout=60
            )
            data = resp.json()
            if 'choices' in data and data['choices']:
                return data['choices'][0]['message']['content']
            return None
        except Exception:
            return None


# ==================== 全局实例 ====================
dm = LegalDocManager()
dm.load_builtin_laws()

# ==================== 文件文本提取 ====================
def extract_text(file_storage, filename):
    lower = filename.lower()
    if lower.endswith('.pdf'):
        import io, PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(file_storage.read()))
        text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        if not text.strip():
            raise ValueError('PDF 无法提取文本（可能是扫描件），请改用 TXT。')
        return text
    elif lower.endswith('.txt'):
        raw = file_storage.read()
        for enc in ('utf-8', 'gbk', 'gb2312', 'gb18030'):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        raise ValueError('TXT 编码无法识别，请使用 UTF-8 或 GBK。')
    else:
        raise ValueError('只支持 .txt 和 .pdf 格式。')

# ==================== API 路由 ====================
@app.route('/api/health')
def health():
    return jsonify(status='ok', docs=len(dm.documents), chunks=len(dm.chunks),
                   laws=dm.law_count, categories=dm.category_count,
                   ai='deepseek' if DEEPSEEK_API_KEY else 'off')

@app.route('/api/stats')
def stats():
    return jsonify(dm.get_stats())

@app.route('/api/websearch_debug')
def websearch_debug():
    info = {'available': WEB_SEARCH_AVAILABLE, 'backend': 'none'}
    if DDGS is not None:
        info['backend'] = getattr(DDGS, '__module__', 'unknown')
    q = request.args.get('q', '劳动合同 试用期 法律')
    try:
        raw_results = web_search(q, max_results=5)
        info['count'] = len(raw_results)
        info['results'] = [{'title': r['title'][:50], 'url': r['url'][:80], 'trusted': r['trusted']} for r in raw_results]
    except Exception as e:
        info['error'] = str(e)[:300]
    return jsonify(info)

@app.route('/api/upload', methods=['POST'])
@login_required
def upload():
    if session.get('username') != DEV_USERNAME:
        return jsonify(error='仅开发者账户可上传文件'), 403
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(error='没有选择文件'), 400
    lower = f.filename.lower()
    if not (lower.endswith('.txt') or lower.endswith('.pdf')):
        return jsonify(error='只支持 .txt 和 .pdf'), 400
    try:
        content = extract_text(f, f.filename)
        store_name = f'upload_{f.filename}'
        dm.add_document(store_name, content)
        art = sum(1 for c in dm.chunks if c['source'] == store_name and c.get('is_article'))
        total = sum(1 for c in dm.chunks if c['source'] == store_name)
        msg = f'"{f.filename}" 上传成功！'
        msg += f'识别 {art} 条法条。' if art else f'切分为 {total} 个段落。'
        return jsonify(success=True, message=msg, filename=store_name, article_count=art, chunk_count=total)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f'上传失败：{e}'), 500

@app.route('/api/documents', methods=['GET'])
@login_required
def list_docs():
    return jsonify(documents=dm.get_doc_list())

@app.route('/api/documents/<path:filename>', methods=['DELETE'])
@login_required
def delete_doc(filename):
    dm.remove_document(filename)
    return jsonify(success=True)

@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.get_json(silent=True)
    if not data or not (data.get('question') or '').strip():
        return jsonify(error='请输入问题'), 400
    question = data['question'].strip()
    # 记录搜索历史
    db = get_db()
    db.execute('INSERT INTO search_history (user_id, question, created_at) VALUES (?,?,?)',
               (session['user_id'], question, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    db.commit()
    mode = data.get('mode', 'ai')
    history = data.get('history', [])
    use_web = bool(data.get('use_web', False))
    use_case = bool(data.get('use_case', False))
    if mode == 'search':
        if use_case:
            result = dm.search_cases_only(question)
        else:
            result = dm.search_only(question)
    else:
        result = dm.answer_with_ai(question, history, use_web, use_case)
    return jsonify(result)

@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    db = get_db()
    rows = db.execute(
        'SELECT id, question, created_at FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 20',
        (session['user_id'],)
    ).fetchall()
    return jsonify(history=[{'id': r['id'], 'q': r['question'], 't': r['created_at']} for r in rows])


@app.route('/api/history/<int:hid>', methods=['DELETE'])
@login_required
def delete_history(hid):
    db = get_db()
    db.execute('DELETE FROM search_history WHERE id=? AND user_id=?', (hid, session['user_id']))
    db.commit()
    return jsonify(success=True)


@app.route('/api/article', methods=['GET'])
@login_required
def get_article():
    source = request.args.get('source', '')
    title = request.args.get('title', '')
    if not source or not title:
        return jsonify(error='缺少参数'), 400
    for chunk in dm.chunks:
        src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
        if chunk['title'] == title and (src_display == source or source in chunk['source']):
            return jsonify(found=True, title=chunk['title'], source=src_display, content=chunk['content'])
    return jsonify(found=False, error='未找到该条文')

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect('/login')
    is_dev = session.get('username') == DEV_USERNAME
    return render_template_string(HTML_TEMPLATE, username=session.get('username', ''), is_dev=is_dev)

# ==================== 登录页模板 ====================
LOGIN_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>登录 - 法律问答助手</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:linear-gradient(135deg,#1a365d 0%,#2c5282 50%,#2b6cb0 100%);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-card{background:#fff;border-radius:16px;padding:44px 38px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
.login-header{text-align:center;margin-bottom:32px}
.login-header .icon{font-size:48px;margin-bottom:12px}
.login-header h1{font-size:22px;color:#1a365d;margin-bottom:6px}
.login-header p{font-size:14px;color:#666}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:14px;color:#333;margin-bottom:7px;font-weight:500}
.form-group input{width:100%;padding:12px 14px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:#4299e1}
.pwd-wrap{position:relative}
.pwd-wrap input{padding-right:44px}
.eye-btn{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;padding:4px;color:#a0aec0;transition:color .2s;display:flex;align-items:center}
.eye-btn:hover{color:#4a5568}
.eye-btn svg{width:20px;height:20px}
.remember-row{display:flex;align-items:center;gap:8px;margin:14px 0;font-size:14px;color:#4a5568;cursor:pointer;user-select:none}
.remember-row input{width:16px;height:16px;cursor:pointer}
.btn-login{width:100%;padding:14px;background:linear-gradient(135deg,#2c5282,#2b6cb0);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:transform .15s,box-shadow .15s}
.btn-login:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 6px 20px rgba(44,82,130,.4)}
.btn-login:disabled{opacity:.7;cursor:not-allowed;transform:none}
.msg{margin-top:14px;padding:11px 14px;border-radius:8px;font-size:13px;display:none}
.msg.error{display:block;background:#fff5f5;border:1px solid #fed7d7;color:#c53030}
.msg.success{display:block;background:#f0fff4;border:1px solid #c6f6d5;color:#276749}
.register-link{text-align:center;margin-top:20px;font-size:14px;color:#718096}
.register-link a{color:#2b6cb0;font-weight:600;text-decoration:none}
.register-link a:hover{text-decoration:underline}
.login-footer{text-align:center;margin-top:14px;font-size:12px;color:#a0aec0}
.loading{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-radius:50%;border-top-color:#fff;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:420px){.login-card{padding:32px 22px}.login-header .icon{font-size:40px}.login-header h1{font-size:19px}}
</style>
</head>
<body>
<div class="login-card">
  <div class="login-header">
    <div class="icon">&#9878;</div>
    <h1>法律问答助手</h1>
    <p>4868部法律法规 · AI智能解答 · 典型案例</p>
  </div>
  <form onsubmit="return false">
    <div class="form-group">
      <label>用户名</label>
      <input type="text" id="username" placeholder="请输入用户名" maxlength="12" autocomplete="username">
    </div>
    <div class="form-group">
      <label>密码</label>
      <div class="pwd-wrap">
        <input type="password" id="password" placeholder="请输入密码" maxlength="30" autocomplete="current-password">
        <button type="button" class="eye-btn" id="eyeBtn" onclick="togglePwd()" title="显示/隐藏密码">
          <svg id="eyeOff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          <svg id="eyeOn" style="display:none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
    </div>
    <label class="remember-row"><input type="checkbox" id="remember" checked> 记住我（30天内免登录）</label>
    <button type="submit" class="btn-login" id="btnLogin" onclick="doLogin()">登 录</button>
  </form>
  <div id="msg" class="msg"></div>
  <div class="register-link">还没有账户？<a href="/register">注册一个</a></div>
  <div class="login-footer">法律问答助手 v5.3 · 安全加密存储</div>
</div>
<script>
document.getElementById('username').onkeydown=function(e){if(e.key==='Enter')document.getElementById('password').focus()};
document.getElementById('password').onkeydown=function(e){if(e.key==='Enter')doLogin()};
function togglePwd(){
  const p=document.getElementById('password'),off=document.getElementById('eyeOff'),on=document.getElementById('eyeOn');
  if(p.type==='password'){p.type='text';off.style.display='none';on.style.display='block'}
  else{p.type='password';off.style.display='block';on.style.display='none'}
}
function showMsg(t,type){const el=document.getElementById('msg');el.textContent=t;el.className='msg '+type}
function hideMsg(){document.getElementById('msg').className='msg'}
async function doLogin(){
  const u=document.getElementById('username').value.trim(),p=document.getElementById('password').value;
  if(!u){showMsg('请输入用户名','error');return}
  if(!p){showMsg('请输入密码','error');return}
  hideMsg();const b=document.getElementById('btnLogin');b.disabled=true;b.innerHTML='<span class="loading"></span>登录中...';
  try{
    const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p,remember:document.getElementById('remember').checked})});
    const d=await r.json();
    if(d.success){showMsg('登录成功，正在进入...','success');setTimeout(()=>location.href='/',600)}
    else{showMsg(d.message,'error');b.disabled=false;b.textContent='登 录'}
  }catch{showMsg('网络错误','error');b.disabled=false;b.textContent='登 录'}
}
</script>
</body>
</html>'''

# ==================== 注册页模板 ====================
REGISTER_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>注册 - 法律问答助手</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:linear-gradient(135deg,#1a365d 0%,#2c5282 50%,#2b6cb0 100%);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.login-card{background:#fff;border-radius:16px;padding:44px 38px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,.25)}
.login-header{text-align:center;margin-bottom:32px}
.login-header .icon{font-size:48px;margin-bottom:12px}
.login-header h1{font-size:22px;color:#1a365d;margin-bottom:6px}
.login-header p{font-size:14px;color:#666}
.form-group{margin-bottom:18px}
.form-group label{display:block;font-size:14px;color:#333;margin-bottom:7px;font-weight:500}
.form-group input{width:100%;padding:12px 14px;border:2px solid #e2e8f0;border-radius:10px;font-size:16px;outline:none;transition:border-color .2s}
.form-group input:focus{border-color:#4299e1}
.form-group .hint{font-size:12px;color:#a0aec0;margin-top:5px}
.pwd-wrap{position:relative}
.pwd-wrap input{padding-right:44px}
.eye-btn{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;padding:4px;color:#a0aec0;transition:color .2s;display:flex;align-items:center}
.eye-btn:hover{color:#4a5568}
.eye-btn svg{width:20px;height:20px}
.btn-login{width:100%;padding:14px;background:linear-gradient(135deg,#2c5282,#2b6cb0);color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:600;cursor:pointer;transition:transform .15s,box-shadow .15s;margin-top:6px}
.btn-login:hover:not(:disabled){transform:translateY(-1px);box-shadow:0 6px 20px rgba(44,82,130,.4)}
.btn-login:disabled{opacity:.7;cursor:not-allowed;transform:none}
.msg{margin-top:14px;padding:11px 14px;border-radius:8px;font-size:13px;display:none}
.msg.error{display:block;background:#fff5f5;border:1px solid #fed7d7;color:#c53030}
.msg.success{display:block;background:#f0fff4;border:1px solid #c6f6d5;color:#276749}
.register-link{text-align:center;margin-top:20px;font-size:14px;color:#718096}
.register-link a{color:#2b6cb0;font-weight:600;text-decoration:none}
.register-link a:hover{text-decoration:underline}
.loading{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.3);border-radius:50%;border-top-color:#fff;animation:spin .7s linear infinite;vertical-align:middle;margin-right:5px}
@keyframes spin{to{transform:rotate(360deg)}}
@media(max-width:420px){.login-card{padding:32px 22px}.login-header .icon{font-size:40px}.login-header h1{font-size:19px}}
</style>
</head>
<body>
<div class="login-card">
  <div class="login-header">
    <div class="icon">&#9878;</div>
    <h1>注册新账户</h1>
    <p>创建账户后即可使用全部功能</p>
  </div>
  <form onsubmit="return false">
    <div class="form-group">
      <label>用户名</label>
      <input type="text" id="username" placeholder="汉字最多6个，英文最多12位" maxlength="12" autocomplete="username">
      <div class="hint">支持汉字、字母、数字（汉字算2个字符）</div>
    </div>
    <div class="form-group">
      <label>密码</label>
      <div class="pwd-wrap">
        <input type="password" id="password" placeholder="设置密码" maxlength="30" autocomplete="new-password">
        <button type="button" class="eye-btn" onclick="togglePwd('password',this)" title="显示/隐藏密码">
          <svg class="eyeOff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          <svg class="eyeOn" style="display:none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
      <div class="hint">6~30位，必须包含大写字母、小写字母和数字</div>
    </div>
    <div class="form-group">
      <label>确认密码</label>
      <div class="pwd-wrap">
        <input type="password" id="password2" placeholder="再次输入密码" maxlength="30" autocomplete="new-password">
        <button type="button" class="eye-btn" onclick="togglePwd('password2',this)" title="显示/隐藏密码">
          <svg class="eyeOff" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
          <svg class="eyeOn" style="display:none" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
    </div>
    <button type="submit" class="btn-login" id="btnReg" onclick="doRegister()">注 册</button>
  </form>
  <div id="msg" class="msg"></div>
  <div class="register-link">已有账户？<a href="/login">去登录</a></div>
</div>
<script>
document.getElementById('username').onkeydown=function(e){if(e.key==='Enter')document.getElementById('password').focus()};
document.getElementById('password').onkeydown=function(e){if(e.key==='Enter')document.getElementById('password2').focus()};
document.getElementById('password2').onkeydown=function(e){if(e.key==='Enter')doRegister()};
function togglePwd(id,btn){
  const p=document.getElementById(id),off=btn.querySelector('.eyeOff'),on=btn.querySelector('.eyeOn');
  if(p.type==='password'){p.type='text';off.style.display='none';on.style.display='block'}
  else{p.type='password';off.style.display='block';on.style.display='none'}
}
function showMsg(t,type){const el=document.getElementById('msg');el.textContent=t;el.className='msg '+type}
function hideMsg(){document.getElementById('msg').className='msg'}
async function doRegister(){
  const u=document.getElementById('username').value.trim(),p=document.getElementById('password').value,p2=document.getElementById('password2').value;
  if(!u){showMsg('请输入用户名','error');return}
  if(!p){showMsg('请输入密码','error');return}
  if(p!==p2){showMsg('两次输入的密码不一致','error');return}
  if(!/[A-Z]/.test(p)){showMsg('密码必须包含至少一个大写字母','error');return}
  if(!/[a-z]/.test(p)){showMsg('密码必须包含至少一个小写字母','error');return}
  if(!/[0-9]/.test(p)){showMsg('密码必须包含至少一个数字','error');return}
  hideMsg();const b=document.getElementById('btnReg');b.disabled=true;b.innerHTML='<span class="loading"></span>注册中...';
  try{
    const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
    const d=await r.json();
    if(d.success){showMsg('注册成功，正在进入...','success');setTimeout(()=>location.href='/',600)}
    else{showMsg(d.message,'error');b.disabled=false;b.textContent='注 册'}
  }catch{showMsg('网络错误','error');b.disabled=false;b.textContent='注 册'}
}
</script>
</body>
</html>'''

# ==================== 主界面模板 ====================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>法律问答助手 - 全量法律库 AI 版</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#eef2f7;color:#333;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .header{background:linear-gradient(135deg,#1a365d,#2c5282);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:12px;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.15)}
  .header .icon{font-size:28px}
  .header h1{font-size:22px;font-weight:600;letter-spacing:2px}
  .header .sub{font-size:13px;opacity:.7;margin-left:8px}
  .header .badge{margin-left:auto;background:rgba(255,255,255,.15);padding:4px 14px;border-radius:20px;font-size:12px}
  .header .user-box{display:flex;align-items:center;gap:10px;margin-left:12px}
  .header .user-phone{font-size:13px;opacity:.9}
  .header .btn-out{background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;padding:4px 12px;border-radius:6px;font-size:12px;cursor:pointer;transition:background .2s}
  .header .btn-out:hover{background:rgba(255,255,255,.25)}
  .main{flex:1;display:flex;overflow:hidden}
  .sidebar{width:300px;background:#fff;border-right:1px solid #e2e8f0;display:flex;flex-direction:column;flex-shrink:0}
  .sidebar-title{padding:18px 20px 10px;font-size:16px;font-weight:600;color:#2d3748}
  .stats-bar{margin:0 16px 8px;padding:10px 14px;background:linear-gradient(135deg,#ebf8ff,#e6fffa);border-radius:10px;font-size:12px;color:#2c5282;line-height:1.6}
  .stats-bar b{color:#1a365d}
  .upload-zone{margin:8px 16px;border:2px dashed #cbd5e0;border-radius:12px;padding:16px;text-align:center;cursor:pointer;transition:all .2s;position:relative}
  .upload-zone:hover,.upload-zone.dragover{border-color:#4299e1;background:#ebf8ff}
  .upload-zone .ui{font-size:28px;color:#a0aec0}
  .upload-zone p{font-size:13px;color:#718096;margin-top:4px}
  .upload-zone small{font-size:12px;color:#a0aec0}
  .upload-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer}
  .upload-status{margin:0 16px;padding:8px;border-radius:8px;font-size:13px;display:none}
  .upload-status.loading{display:block;background:#fffbeb;color:#92400e}
  .upload-status.success{display:block;background:#f0fff4;color:#276749}
  .upload-status.error{display:block;background:#fff5f5;color:#9b2c2c}
  .doc-list{flex:1;overflow-y:auto;padding:8px 16px}
  .doc-item{display:flex;align-items:center;padding:10px 12px;margin-bottom:6px;background:#f7fafc;border-radius:10px;gap:10px}
  .doc-item:hover{background:#edf2f7}
  .doc-item .di{font-size:20px;flex-shrink:0}
  .doc-item .info{flex:1;min-width:0}
  .doc-item .dn{font-size:13px;font-weight:500;color:#2d3748;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .doc-item .dm{font-size:11px;color:#a0aec0}
  .doc-item .dd{background:none;border:none;font-size:16px;color:#e53e3e;cursor:pointer;padding:4px 8px;border-radius:6px}
  .doc-item .dd:hover{background:#fff5f5}
  .history-section{margin:8px 16px;border-top:1px solid #e2e8f0;padding-top:10px}
  .history-title{font-size:13px;font-weight:600;color:#4a5568;margin-bottom:8px}
  .history-list{max-height:180px;overflow-y:auto}
  .history-empty{font-size:12px;color:#a0aec0;text-align:center;padding:8px}
  .history-item{display:flex;align-items:center;gap:8px;padding:7px 10px;margin-bottom:4px;background:#f7fafc;border-radius:8px;cursor:pointer;transition:background .15s;font-size:13px;color:#4a5568}
  .history-item:hover{background:#ebf8ff;color:#2b6cb0}
  .history-item .hq{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}
  .history-item .ht{font-size:11px;color:#a0aec0;flex-shrink:0}
  .history-item .hd{background:none;border:none;font-size:13px;color:#cbd5e0;cursor:pointer;padding:2px 5px;border-radius:4px;flex-shrink:0;transition:all .15s}
  .history-item .hd:hover{color:#e53e3e;background:#fff5f5}
  .flk-link{margin:8px 16px 12px;padding:10px;background:#f0fff4;border-radius:10px;text-align:center}
  .flk-link a{color:#276749;font-size:13px;text-decoration:none;font-weight:500}
  .flk-link a:hover{text-decoration:underline}
  .chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .messages{flex:1;overflow-y:auto;padding:24px 32px}
  .welcome{text-align:center;margin-top:50px;color:#a0aec0}
  .welcome .wi{font-size:60px;margin-bottom:14px}
  .welcome h2{font-size:20px;color:#4a5568;margin-bottom:10px}
  .welcome p{font-size:14px;line-height:1.8;max-width:480px;margin:0 auto}
  .welcome .tips{margin-top:24px;display:flex;flex-wrap:wrap;justify-content:center;gap:10px}
  .welcome .tt{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:8px 16px;font-size:13px;color:#4a5568;cursor:pointer;transition:all .15s}
  .welcome .tt:hover{background:#ebf8ff;border-color:#4299e1;color:#2b6cb0}
  .msg-row{display:flex;margin-bottom:20px;gap:10px}
  .msg-row.user{justify-content:flex-end}
  .msg-row.bot{justify-content:flex-start}
  .msg-avatar{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:17px;flex-shrink:0}
  .msg-row.user .msg-avatar{background:#4299e1;color:#fff}
  .msg-row.bot .msg-avatar{background:#e2e8f0}
  .msg-bubble{max-width:75%;padding:14px 18px;border-radius:16px;font-size:14px;line-height:1.8;white-space:pre-wrap;word-break:break-word}
  .msg-row.user .msg-bubble{background:#4299e1;color:#fff;border-bottom-right-radius:4px}
  .msg-row.bot .msg-bubble{background:#fff;color:#2d3748;border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.06)}
  .ai-badge{display:inline-block;background:#f0fff4;color:#276749;font-size:11px;padding:2px 8px;border-radius:8px;margin-bottom:8px}
  .typing-indicator span{display:inline-block;width:8px;height:8px;background:#a0aec0;border-radius:50%;margin:0 2px;animation:bounce 1.4s infinite ease-in-out}
  .typing-indicator span:nth-child(1){animation-delay:0s}
  .typing-indicator span:nth-child(2){animation-delay:.2s}
  .typing-indicator span:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,80%,100%{transform:scale(.6);opacity:.4}40%{transform:scale(1);opacity:1}}
  .citation-tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
  .citation-tag{background:#ebf8ff;color:#2b6cb0;font-size:11px;padding:3px 10px;border-radius:12px;cursor:pointer;transition:all .15s;border:1px solid transparent;text-decoration:none}
  .citation-tag:hover{background:#bee3f8;border-color:#4299e1;text-decoration:underline}
  .input-area{padding:16px 32px 20px;background:#fff;border-top:1px solid #e2e8f0;display:flex;gap:12px;align-items:flex-end}
  .input-area textarea{flex:1;border:2px solid #e2e8f0;border-radius:12px;padding:12px 16px;font-size:14px;font-family:inherit;resize:none;outline:none;max-height:120px;line-height:1.5;transition:border-color .15s}
  .input-area textarea:focus{border-color:#4299e1}
  .input-area .send-btn{background:linear-gradient(135deg,#4299e1,#3182ce);color:#fff;border:none;border-radius:12px;padding:12px 24px;font-size:14px;font-weight:600;cursor:pointer;transition:all .15s;white-space:nowrap}
  .input-area .send-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(66,153,225,.4)}
  .input-area .send-btn:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}
  .input-area .send-btn.stop{background:linear-gradient(135deg,#e53e3e,#c53030);animation:pulse 1.5s infinite}
  .input-area .send-btn.stop:hover{box-shadow:0 4px 12px rgba(229,62,62,.4)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.8}}
  .mode-toggle{display:flex;align-items:center;gap:6px;margin-bottom:8px;font-size:12px;color:#718096;user-select:none}
  .mode-toggle .mt-label{cursor:pointer;padding:4px 12px;border-radius:16px;border:1.5px solid #e2e8f0;transition:all .15s;background:#fff}
  .mode-toggle .mt-label:hover{border-color:#4299e1;color:#2b6cb0}
  .mode-toggle .mt-label.active{background:#4299e1;color:#fff;border-color:#4299e1;font-weight:600}
  .mode-toggle .mt-label.active-search{background:#38a169;color:#fff;border-color:#38a169;font-weight:600}
  .mode-toggle .mt-label.active-web{background:#805ad5;color:#fff;border-color:#805ad5;font-weight:600}
  .mode-toggle .mt-label.active-case{background:#d69e2e;color:#fff;border-color:#d69e2e;font-weight:600}
  .mode-toggle .mt-hint{font-size:11px;color:#a0aec0;margin-left:4px}
  ::-webkit-scrollbar{width:6px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#cbd5e0;border-radius:3px}
  /* ===== 移动端组件基础样式（桌面隐藏） ===== */
  .menu-btn{display:none;align-items:center;justify-content:center;width:38px;height:38px;background:rgba(255,255,255,.15);border:1px solid rgba(255,255,255,.3);color:#fff;border-radius:8px;font-size:18px;cursor:pointer;flex-shrink:0;line-height:1}
  .backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:90;opacity:0;transition:opacity .25s}
  .backdrop.show{display:block;opacity:1}
  .sidebar-close{display:none;position:absolute;top:12px;right:12px;z-index:5;align-items:center;justify-content:center;width:32px;height:32px;background:#f7fafc;border:1px solid #e2e8f0;border-radius:8px;font-size:15px;color:#4a5568;cursor:pointer;line-height:1}
  /* ===== 移动端响应式（≤768px） ===== */
  @media(max-width:768px){
    .menu-btn{display:flex}
    .header{padding:10px 14px;gap:8px}
    .header .icon{font-size:22px}
    .header h1{font-size:17px;letter-spacing:1px}
    .header .sub,.header .badge{display:none}
    .header .user-box{margin-left:auto;gap:6px}
    .header .user-phone{font-size:12px}
    .header .btn-out{padding:3px 10px;font-size:11px}
    .main{flex-direction:column}
    /* 侧边栏变为抽屉 */
    .sidebar{position:fixed;left:0;top:0;bottom:0;width:82%;max-width:320px;max-height:none;z-index:100;transform:translateX(-105%);transition:transform .28s ease;border-right:none;box-shadow:none;padding-top:4px}
    .sidebar.open{transform:translateX(0);box-shadow:6px 0 24px rgba(0,0,0,.25)}
    .sidebar-close{display:flex}
    .sidebar-title{padding-right:52px}
    .messages{padding:16px 12px}
    .input-area{padding:8px 10px calc(10px + env(safe-area-inset-bottom));gap:8px}
    .input-area > div{min-width:0}
    .input-area textarea{font-size:16px;padding:8px 10px;max-height:80px}
    .input-area .send-btn{padding:9px 12px;font-size:12px;flex-shrink:0}
    .msg-bubble{max-width:90%;font-size:14px;padding:12px 14px}
    .msg-avatar{width:30px;height:30px;font-size:14px}
    .msg-row{margin-bottom:14px;gap:8px}
    .welcome{margin-top:24px}
    .welcome .wi{font-size:44px;margin-bottom:10px}
    .welcome h2{font-size:17px}
    .welcome p{font-size:13px;line-height:1.7}
    .welcome .tips{gap:8px;margin-top:18px}
    .welcome .tt{font-size:12px;padding:7px 12px}
    /* 模式切换自动换行，说明文字独占一行始终可见 */
    .mode-toggle{flex-wrap:wrap;overflow-x:visible;gap:5px;padding-bottom:2px;margin-bottom:6px}
    .mode-toggle .mt-label{white-space:nowrap;flex-shrink:0;font-size:12px;padding:4px 9px}
    .mode-toggle .mt-hint{flex-basis:100%;width:100%;font-size:11px;margin-left:0;margin-top:1px}
    .doc-item .dn{font-size:12px}
    .doc-item .dm{font-size:10px}
    .history-item{font-size:12px;padding:6px 8px}
    .history-item .ht{font-size:10px}
    .citation-tag{font-size:10px;padding:3px 8px}
    .citation-tags{gap:5px}
    .stats-bar{font-size:11px}
    .flk-link a{font-size:12px}
  }
</style>
</head>
<body>
<div class="header">
  <button class="menu-btn" onclick="toggleSidebar()" aria-label="打开菜单">&#9776;</button>
  <span class="icon">&#9878;</span>
  <h1>法律问答助手</h1>
  <span class="sub">DeepSeek AI · 4868部法律法规 + 司法解释 + 典型案例</span>
  <span class="badge">全量法律库 v5.3</span>
  <div class="user-box">
    <span class="user-phone">&#128100; {{ username }}</span>
    <button class="btn-out" onclick="logout()">退出</button>
  </div>
</div>
<div class="backdrop" id="backdrop" onclick="toggleSidebar()"></div>
<div class="main">
  <div class="sidebar" id="sidebar">
    <button class="sidebar-close" onclick="toggleSidebar()" aria-label="关闭菜单">&#10005;</button>
    <div class="sidebar-title">&#128218; 法律知识库</div>
    <div class="stats-bar" id="statsBar">正在加载法律库统计…</div>
    {% if is_dev %}
    <div class="upload-zone" id="uploadZone">
      <div class="ui">&#128228;</div>
      <p>上传更多法律文件</p>
      <small>.txt / .pdf</small>
      <input type="file" id="fileInput" accept=".txt,.pdf" multiple>
    </div>
    <div class="upload-status" id="uploadStatus"></div>
    {% endif %}
    <div class="doc-list" id="docList"></div>
    <div class="history-section">
      <div class="history-title">&#128336; 搜索记录</div>
      <div class="history-list" id="historyList"><div class="history-empty">暂无搜索记录</div></div>
    </div>
    <div class="flk-link">
      <a href="https://flk.npc.gov.cn/" target="_blank">&#128279; 在国家法律法规数据库中搜索</a>
    </div>
    <div class="flk-link" style="margin-top:0">
      <a href="https://rmfyalk.court.gov.cn/" target="_blank">&#9878; 在人民法院案例库中检索案例</a>
    </div>
  </div>
  <div class="chat-area">
    <div class="messages" id="messages">
      <div class="welcome" id="welcome">
        <div class="wi">&#9878;</div>
        <h2>你好，我是法小智</h2>
        <p>我已内置 <b>4868部</b> 法律法规、司法解释及典型案例，涵盖宪法、民法典、刑法、行政法、经济法、社会法、诉讼法等全部法律门类，由 DeepSeek AI 驱动。<br>直接输入法律问题即可获得智能解答，也可搜索两高典型案例。</p>
        <div class="tips">
          <span class="tt" onclick="fill('什么是正当防卫？')">什么是正当防卫？</span>
          <span class="tt" onclick="fill('合同违约怎么赔偿？')">合同违约怎么赔偿？</span>
          <span class="tt" onclick="fill('离婚财产如何分割？')">离婚财产如何分割？</span>
          <span class="tt" onclick="fill('劳动者被辞退有什么补偿？')">被辞退有什么补偿？</span>
          <span class="tt" onclick="toggleCase();fill('正当防卫的典型案例有哪些？')">正当防卫典型案例</span>
          <span class="tt" onclick="toggleCase();fill('劳动争议典型案例')">劳动争议典型案例</span>
        </div>
      </div>
    </div>
    <div class="input-area">
      <div style="flex:1;display:flex;flex-direction:column">
        <div class="mode-toggle">
          <span class="mt-label active" id="modeAi" onclick="setMode('ai')">&#129302; AI智能回答</span>
          <span class="mt-label" id="modeSearch" onclick="setMode('search')">&#128269; 直接搜索</span>
          <span class="mt-label" id="modeCase" onclick="toggleCase()" title="开启后搜索两高典型案例（可搭配AI或单独使用）">&#9878; 案例搜索</span>
          <span class="mt-label" id="modeWeb" onclick="toggleWeb()" title="开启后同时联网搜索权威法律信息（已筛选）">&#127760; 联网搜索</span>
          <span class="mt-hint" id="modeHint">AI回答更精准，消耗tokens</span>
        </div>
        <textarea id="qInput" rows="1" placeholder="请输入您的法律问题..." onkeydown="hk(event)"></textarea>
      </div>
      <button class="send-btn" id="sendBtn" onclick="send()">发送提问</button>
    </div>
  </div>
</div>
<script>
let busy=false,chatMode='ai',abortCtrl=null,chatHistory=[],useWeb=false,useCase=false;
function toggleSidebar(){const sb=document.getElementById('sidebar'),bd=document.getElementById('backdrop');const open=sb.classList.toggle('open');bd.classList.toggle('show',open);document.body.style.overflow=open?'hidden':''}
function toggleWeb(){useWeb=!useWeb;document.getElementById('modeWeb').className=useWeb?'mt-label active-web':'mt-label'}
function toggleCase(){useCase=!useCase;document.getElementById('modeCase').className=useCase?'mt-label active-case':'mt-label'}
function setMode(m){chatMode=m;const ai=document.getElementById('modeAi'),se=document.getElementById('modeSearch'),hint=document.getElementById('modeHint');ai.className='mt-label';se.className='mt-label';if(m==='ai'){ai.className='mt-label active';hint.textContent='AI回答更精准，消耗tokens'}else{se.className='mt-label active-search';hint.textContent='直接搜索法律库，免费不消耗tokens'}}
function setBtnStop(s){const b=document.getElementById('sendBtn');if(s){b.textContent='\u23F9 停止';b.className='send-btn stop';b.onclick=cancelSend}else{b.textContent='发送提问';b.className='send-btn';b.onclick=send}}
function cancelSend(){if(abortCtrl){abortCtrl.abort();abortCtrl=null}}
async function logout(){try{await fetch('/api/logout',{method:'POST'})}catch{};location.href='/login'}
window.addEventListener('DOMContentLoaded',()=>{loadDocs();loadStats();loadHistory();ar()});
const fi=document.getElementById('fileInput'),uz=document.getElementById('uploadZone'),us=document.getElementById('uploadStatus');
if(fi&&uz&&us){
fi.addEventListener('change',e=>{if(e.target.files.length)upAll(e.target.files)});
uz.addEventListener('dragover',e=>{e.preventDefault();uz.classList.add('dragover')});
uz.addEventListener('dragleave',()=>uz.classList.remove('dragover'));
uz.addEventListener('drop',e=>{e.preventDefault();uz.classList.remove('dragover');if(e.dataTransfer.files.length)upAll(e.dataTransfer.files)});
}
async function upAll(files){for(const f of files)await upOne(f);if(fi)fi.value='';loadDocs()}
async function upOne(file){ss('loading','正在上传 "'+file.name+'"…');const fd=new FormData();fd.append('file',file);try{const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();if(r.ok&&d.success)ss('success',d.message);else ss('error',d.error||'上传失败')}catch{ss('error','网络错误')}}
function ss(t,m){us.className='upload-status '+t;us.textContent=m;if(t==='success')setTimeout(()=>us.className='upload-status',5000)}
async function loadStats(){try{const r=await fetch('/api/stats');const d=await r.json();const cats=Object.entries(d.categories||{}).sort((a,b)=>b[1]-a[1]).slice(0,6).map(e=>e[0]).join('、');document.getElementById('statsBar').innerHTML='<b>'+d.total+'</b> 部法律法规 · <b>'+d.chunks.toLocaleString()+'</b> 个条文<br>涵盖：'+cats+' 等'}catch{document.getElementById('statsBar').textContent='法律库已就绪'}}
async function loadDocs(){try{const r=await fetch('/api/documents');if(r.status===401){location.href='/login';return}const d=await r.json();rdl(d.documents)}catch{}}
function rdl(docs){const el=document.getElementById('docList');const uploads=docs.filter(d=>!d.builtin);if(!uploads.length){el.innerHTML='';return}el.innerHTML='<div style="padding:4px 0 8px;font-size:12px;color:#718096">用户上传 ('+uploads.length+')</div>'+uploads.map(d=>{const icon=d.name.endsWith('.pdf')?'&#128211;':'&#128196;';const dn=d.name.replace(/^upload_/,'');return `<div class="doc-item"><span class="di">${icon}</span><div class="info"><div class="dn" title="${dn}">${dn}</div><div class="dm">${d.articles>0?d.articles+' 条法条':d.chunks+' 个段落'}</div></div><button class="dd" onclick="dd('${d.name}')" title="删除">&#10005;</button></div>`}).join('')}
async function loadHistory(){try{const r=await fetch('/api/history');if(!r.ok)return;const d=await r.json();const el=document.getElementById('historyList');if(!d.history||!d.history.length){el.innerHTML='<div class="history-empty">暂无搜索记录</div>';return}el.innerHTML=d.history.map(h=>{const t=h.t?h.t.slice(5,16):'';return `<div class="history-item"><span class="hq" onclick="fill('${esc(h.q).replace(/'/g,"\\'")}')">${esc(h.q)}</span><span class="ht">${t}</span><button class="hd" onclick="delHistory(${h.id})" title="删除">&#10005;</button></div>`}).join('')}catch{}}
async function delHistory(id){try{await fetch('/api/history/'+id,{method:'DELETE'});loadHistory()}catch{}}
async function dd(n){if(!confirm('确定删除吗？'))return;try{await fetch('/api/documents/'+encodeURIComponent(n),{method:'DELETE'});loadDocs()}catch{alert('删除失败')}}
function fill(t){document.getElementById('qInput').value=t;document.getElementById('qInput').focus();const sb=document.getElementById('sidebar');if(sb&&sb.classList.contains('open'))toggleSidebar()}
function hk(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function ar(){const t=document.getElementById('qInput');t.addEventListener('input',()=>{t.style.height='auto';t.style.height=Math.min(t.scrollHeight,120)+'px'})}
async function send(){
  if(busy)return;const inp=document.getElementById('qInput'),q=inp.value.trim();if(!q)return;
  const w=document.getElementById('welcome');if(w)w.style.display='none';
  addMsg('user',q);inp.value='';inp.style.height='auto';
  chatHistory.push({role:'user',content:q});
  busy=true;setBtnStop(true);abortCtrl=new AbortController();const ld=addTyping();
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,mode:chatMode,history:chatHistory.slice(0,-1),use_web:useWeb,use_case:useCase}),signal:abortCtrl.signal});
    if(r.status===401){ld.remove();location.href='/login';return}
    const d=await r.json();ld.remove();
    if(d.answer){addMsg('bot',d.answer,d.citations,d.ai_used,d.web_citations);chatHistory.push({role:'assistant',content:d.answer})}
    else if(d.error)addMsg('bot','错误：'+d.error)}
  catch(e){ld.remove();if(e.name==='AbortError'){addMsg('bot','\u23F9 已取消提问，未消耗tokens。',[],false);chatHistory.pop()}else addMsg('bot','网络错误，请稍后再试。')}
  busy=false;abortCtrl=null;setBtnStop(false);loadHistory()
}
function addMsg(role,text,cites,aiUsed,webCites){
  const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row '+role;
  const av=role==='user'?'&#128100;':'&#9878;';
  let aiBadge=(aiUsed&&role==='bot')?'<div class="ai-badge">&#129302; DeepSeek AI 回答</div>':'';
  let ch='';if(cites&&cites.length)ch='<div class="citation-tags">'+cites.map(c=>'<span class="citation-tag" onclick="showArticle(\''+esc(c.title).replace(/'/g,"\\'")+'\',\''+esc(c.source).replace(/'/g,"\\'")+'\')" title="点击查看原文">'+esc(c.title+' · '+c.source.replace(/^upload_/,''))+'</span>').join('')+'</div>';
  let wch='';if(webCites&&webCites.length)wch='<div class="citation-tags">'+webCites.map(w=>'<a class="citation-tag" href="'+esc(w.url)+'" target="_blank" title="'+esc(w.url)+'">&#127760; '+esc(w.title)+'</a>').join('')+'</div>';
  r.innerHTML=(role==='user'?'':'<div class="msg-avatar">'+av+'</div>')+'<div class="msg-bubble">'+aiBadge+esc(text)+ch+wch+'</div>'+(role==='user'?'<div class="msg-avatar">'+av+'</div>':'');
  c.appendChild(r);c.scrollTop=c.scrollHeight
}
function addTyping(){const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row bot';r.innerHTML='<div class="msg-avatar">&#9878;</div><div class="msg-bubble typing-indicator"><span></span><span></span><span></span></div>';c.appendChild(r);c.scrollTop=c.scrollHeight;return r}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function showArticle(title,source){
  if(busy)return;busy=true;const ld=addTyping();
  try{const r=await fetch('/api/article?title='+encodeURIComponent(title)+'&source='+encodeURIComponent(source));const d=await r.json();ld.remove();if(d.found)addMsg('bot','\uD83D\uDCDC '+d.title+'（'+d.source+'）\n\n'+d.content,[],false);else addMsg('bot','抱歉，未能找到该条文原文。',[],false)}
  catch{ld.remove();addMsg('bot','网络错误，请稍后再试。',[],false)}
  busy=false;
}
</script>
</body>
</html>'''

# ==================== 启动 ====================
init_db()

# 确保开发者账户存在
_conn = sqlite3.connect(DB_PATH)
_exists = _conn.execute('SELECT id FROM users WHERE username=?', (DEV_USERNAME,)).fetchone()
if not _exists:
    _conn.execute('INSERT INTO users (username, password_hash, created_at, last_login, login_count) VALUES (?,?,?,?,0)',
                  (DEV_USERNAME, generate_password_hash(DEV_PASSWORD), datetime.now().strftime('%Y-%m-%d %H:%M:%S'), None))
    _conn.commit()
    print(f'[用户系统] 已创建开发者账户: {DEV_USERNAME}')
_conn.close()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print()
    print('=' * 50)
    print('    法律问答助手 v5.1 (全量法律库 + AI + 案例搜索 + 登录)')
    print(f'    内置法律: {dm.law_count} 部, {len(dm.chunks)} 个条文')
    print(f'    AI 引擎:  {"DeepSeek" if DEEPSEEK_API_KEY else "未配置"}')
    print(f'    用户系统: 用户名密码登录 + 注册')
    print(f'    访问:     http://127.0.0.1:{port}')
    print('=' * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
