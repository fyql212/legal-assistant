# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║       法律问答助手 — Web 云端版 v2.0                  ║
║                                                      ║
║  部署平台：Render.com（免费）                          ║
║  技术栈：  Python + Flask + jieba + PyPDF2             ║
║  特点：    内存存储，无磁盘依赖，适配云端环境            ║
╚══════════════════════════════════════════════════════╝
"""

import os
import re
import json
import uuid
from flask import Flask, request, jsonify, render_template_string

# ---- 依赖导入（首次运行会自动安装） ----
try:
    import jieba
    import PyPDF2
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'jieba', 'PyPDF2'])
    import jieba
    import PyPDF2

# ==================== Flask 配置 ====================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024  # 最大 30MB
app.secret_key = os.environ.get('SECRET_KEY', 'legal-assistant-secret-key-2024')

# ==================== 内存文档管理器 ====================
class LegalDocManager:
    """纯内存文档管理，适合云端无持久化存储的场景"""

    def __init__(self):
        self.documents = {}   # {文件名: 原文}
        self.chunks = []

    def add_document(self, filename, content):
        self.documents[filename] = content
        self._rebuild_chunks()

    def remove_document(self, filename):
        self.documents.pop(filename, None)
        self._rebuild_chunks()

    def clear_all(self):
        self.documents.clear()
        self.chunks.clear()

    def get_doc_list(self):
        result = []
        for name in self.documents:
            art = sum(1 for c in self.chunks if c['source'] == name and c.get('is_article'))
            total = sum(1 for c in self.chunks if c['source'] == name)
            result.append({'name': name, 'articles': art, 'chunks': total})
        return result

    # ---------- 文档切分 ----------
    def _rebuild_chunks(self):
        self.chunks = []
        for filename, content in self.documents.items():
            self.chunks.extend(self._split_content(content, filename))

    def _split_content(self, content, filename):
        chunks = []
        pattern = r'第([零一二三四五六七八九十百千\d]+)条'
        matches = list(re.finditer(pattern, content))

        if len(matches) >= 3:
            if matches[0].start() > 0:
                header = content[:matches[0].start()].strip()
                if header and len(header) > 10:
                    chunks.append(self._make_chunk('前言/目录', header[:2000], filename, False, ''))

            for i, match in enumerate(matches):
                num = match.group(1)
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                text = content[start:end].strip()
                if text and len(text) > 5:
                    chunks.append(self._make_chunk(f'第{num}条', text, filename, True, num))
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
                            chunks.append(self._make_chunk(f'段落{idx}', seg, filename, False, ''))
                else:
                    chunks.append(self._make_chunk(f'段落{idx}', p, filename, False, ''))
        return chunks

    @staticmethod
    def _make_chunk(title, content, source, is_article, article_num):
        return {'title': title, 'content': content, 'source': source,
                'is_article': is_article, 'article_num': article_num}

    # ---------- 中文数字转整数 ----------
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

    # ---------- 关键词提取 ----------
    _STOP = {
        '的','了','是','在','我','有','和','就','不','人','都','一','一个','上','也','很','到',
        '说','要','去','你','会','着','没有','看','好','自己','这','他','她','吗','呢','什么',
        '怎么','如何','请问','请','哪些','哪','那','这个','那个','可以','能','啊','吧','把','被',
        '给','让','从','对','向','与','及','等','中','下','时','为','以','之','所','其','而',
        '但','又','或','如果','因为','所以','但是','而且',
    }

    def _keywords(self, text):
        words = jieba.lcut(text)
        seen = set()
        result = []
        for w in words:
            w = w.strip()
            if len(w) >= 2 and w not in self._STOP and w not in seen and not re.match(r'^[^\u4e00-\u9fff\w]+$', w):
                seen.add(w)
                result.append(w)
        return result

    # ---------- 搜索 ----------
    def search(self, question, top_n=5):
        kws = self._keywords(question)
        if not kws:
            return []

        asked_nums = set()
        for m in re.finditer(r'第([零一二三四五六七八九十百千\d]+)条', question):
            asked_nums.add(self._cn2int(m.group(1)))

        scored = []
        for chunk in self.chunks:
            score = 0.0
            combined = chunk['content'] + ' ' + chunk['title']
            for kw in kws:
                cnt = combined.count(kw)
                if cnt:
                    score += cnt * (1.5 if len(kw) >= 3 else 1.0)
            if chunk['is_article']:
                cn = self._cn2int(chunk['article_num'])
                if cn in asked_nums:
                    score += 50
            if score > 0:
                scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_n]]

    # ---------- 生成回答 ----------
    def answer(self, question):
        kws = self._keywords(question)
        results = self.search(question)

        if not self.chunks:
            return {'answer': '还没有上传任何法律文件！请先上传 TXT 或 PDF 格式的法律文件，然后再提问。',
                    'citations': [], 'has_result': False}

        if not results:
            return {
                'answer': ('抱歉，在已上传的法律文件中没有找到与您问题直接相关的内容。\n\n'
                           '建议尝试：\n• 换一种方式描述问题\n• 使用更具体的法律术语\n'
                           '• 直接引用法条号（如"第577条"）\n• 上传更多法律文件'),
                'citations': [], 'has_result': False}

        parts, cites = [], []
        for chunk in results:
            display = chunk['content'][:500] + ('……' if len(chunk['content']) > 500 else '')
            parts.append(f"【{chunk['title']}】（来源：{chunk['source']}）\n{display}")
            cites.append({'title': chunk['title'], 'source': chunk['source']})

        kw_str = '、'.join(kws[:6]) if kws else ''
        header = f"根据您的问题，找到以下 {len(results)} 条相关内容"
        if kw_str:
            header += f"（关键词：{kw_str}）"
        header += '：\n\n'

        return {'answer': header + '\n\n'.join(parts), 'citations': cites, 'has_result': True}


# ==================== 全局实例 ====================
dm = LegalDocManager()


# ==================== 文件文本提取 ====================
def extract_text(file_storage, filename):
    lower = filename.lower()

    if lower.endswith('.pdf'):
        import io
        reader = PyPDF2.PdfReader(io.BytesIO(file_storage.read()))
        text = '\n'.join(p.extract_text() or '' for p in reader.pages)
        if not text.strip():
            raise ValueError('此 PDF 无法提取文本（可能是扫描件），请改用 TXT 格式。')
        return text

    elif lower.endswith('.txt'):
        raw = file_storage.read()
        for enc in ('utf-8', 'gbk', 'gb2312', 'gb18030'):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        raise ValueError('TXT 文件编码无法识别，请使用 UTF-8 或 GBK 编码。')

    else:
        raise ValueError(f'不支持的格式，请上传 .txt 或 .pdf 文件。')


# ==================== API 路由 ====================
@app.route('/api/health')
def health():
    return jsonify(status='ok', docs=len(dm.documents), chunks=len(dm.chunks))

@app.route('/api/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(error='没有选择文件'), 400
    lower = f.filename.lower()
    if not (lower.endswith('.txt') or lower.endswith('.pdf')):
        return jsonify(error='只支持 .txt 和 .pdf 格式'), 400
    try:
        content = extract_text(f, f.filename)
        dm.add_document(f.filename, content)
        art = sum(1 for c in dm.chunks if c['source'] == f.filename and c.get('is_article'))
        total = sum(1 for c in dm.chunks if c['source'] == f.filename)
        msg = f'"{f.filename}" 上传成功！'
        msg += f'共识别 {art} 条法条。' if art else f'共切分为 {total} 个段落。'
        return jsonify(success=True, message=msg, filename=f.filename, article_count=art, chunk_count=total)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=f'上传失败：{e}'), 500

@app.route('/api/documents', methods=['GET'])
def list_docs():
    return jsonify(documents=dm.get_doc_list())

@app.route('/api/documents/<path:filename>', methods=['DELETE'])
def delete_doc(filename):
    dm.remove_document(filename)
    return jsonify(success=True)

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.get_json(silent=True)
    if not data or not (data.get('question') or '').strip():
        return jsonify(error='请输入您的问题'), 400
    result = dm.answer(data['question'].strip())
    return jsonify(result)


# ==================== 前端页面 ====================
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)


HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>法律问答助手</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#eef2f7;color:#333;height:100vh;display:flex;flex-direction:column;overflow:hidden}

  /* ====== 顶栏 ====== */
  .header{background:linear-gradient(135deg,#1a365d,#2c5282);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:12px;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.15)}
  .header .icon{font-size:28px}
  .header h1{font-size:22px;font-weight:600;letter-spacing:2px}
  .header .sub{font-size:13px;opacity:.7;margin-left:8px}
  .header .badge{margin-left:auto;background:rgba(255,255,255,.15);padding:4px 14px;border-radius:20px;font-size:12px}

  /* ====== 主体 ====== */
  .main{flex:1;display:flex;overflow:hidden}

  /* ====== 侧栏 ====== */
  .sidebar{width:300px;background:#fff;border-right:1px solid #e2e8f0;display:flex;flex-direction:column;flex-shrink:0}
  .sidebar-title{padding:18px 20px 10px;font-size:16px;font-weight:600;color:#2d3748}

  .upload-zone{margin:8px 16px;border:2px dashed #cbd5e0;border-radius:12px;padding:24px 16px;text-align:center;cursor:pointer;transition:all .2s;position:relative}
  .upload-zone:hover,.upload-zone.dragover{border-color:#4299e1;background:#ebf8ff}
  .upload-zone .ui{font-size:36px;color:#a0aec0}
  .upload-zone p{font-size:14px;color:#718096;margin-top:8px}
  .upload-zone small{font-size:12px;color:#a0aec0}
  .upload-zone input[type=file]{position:absolute;inset:0;opacity:0;cursor:pointer}

  .upload-status{margin:0 16px;padding:10px;border-radius:8px;font-size:13px;display:none}
  .upload-status.loading{display:block;background:#fffbeb;color:#92400e}
  .upload-status.success{display:block;background:#f0fff4;color:#276749}
  .upload-status.error{display:block;background:#fff5f5;color:#9b2c2c}

  .doc-list{flex:1;overflow-y:auto;padding:8px 16px}
  .doc-item{display:flex;align-items:center;padding:10px 12px;margin-bottom:6px;background:#f7fafc;border-radius:10px;gap:10px;transition:background .15s}
  .doc-item:hover{background:#edf2f7}
  .doc-item .di{font-size:22px;flex-shrink:0}
  .doc-item .info{flex:1;min-width:0}
  .doc-item .dn{font-size:14px;font-weight:500;color:#2d3748;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .doc-item .dm{font-size:12px;color:#a0aec0}
  .doc-item .dd{background:none;border:none;font-size:18px;color:#e53e3e;cursor:pointer;padding:4px 8px;border-radius:6px;transition:background .15s}
  .doc-item .dd:hover{background:#fff5f5}
  .doc-empty{text-align:center;padding:20px;color:#a0aec0;font-size:14px}

  /* ====== 聊天区 ====== */
  .chat-area{flex:1;display:flex;flex-direction:column;overflow:hidden}
  .messages{flex:1;overflow-y:auto;padding:24px 32px}

  .welcome{text-align:center;margin-top:60px;color:#a0aec0}
  .welcome .wi{font-size:64px;margin-bottom:16px}
  .welcome h2{font-size:20px;color:#4a5568;margin-bottom:12px}
  .welcome p{font-size:15px;line-height:1.8;max-width:420px;margin:0 auto}
  .welcome .tips{margin-top:28px;display:flex;flex-wrap:wrap;justify-content:center;gap:10px}
  .welcome .tt{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:8px 18px;font-size:14px;color:#4a5568;cursor:pointer;transition:all .15s}
  .welcome .tt:hover{background:#ebf8ff;border-color:#4299e1;color:#2b6cb0}

  .msg-row{display:flex;margin-bottom:20px;gap:10px}
  .msg-row.user{justify-content:flex-end}
  .msg-row.bot{justify-content:flex-start}
  .msg-avatar{width:38px;height:38px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
  .msg-row.user .msg-avatar{background:#4299e1;color:#fff}
  .msg-row.bot .msg-avatar{background:#e2e8f0}
  .msg-bubble{max-width:72%;padding:14px 18px;border-radius:16px;font-size:15px;line-height:1.75;white-space:pre-wrap;word-break:break-word}
  .msg-row.user .msg-bubble{background:#4299e1;color:#fff;border-bottom-right-radius:4px}
  .msg-row.bot .msg-bubble{background:#fff;color:#2d3748;border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.06)}

  .typing-indicator span{display:inline-block;width:8px;height:8px;background:#a0aec0;border-radius:50%;margin:0 2px;animation:bounce 1.4s infinite ease-in-out}
  .typing-indicator span:nth-child(1){animation-delay:0s}
  .typing-indicator span:nth-child(2){animation-delay:.2s}
  .typing-indicator span:nth-child(3){animation-delay:.4s}
  @keyframes bounce{0%,80%,100%{transform:scale(.6);opacity:.4}40%{transform:scale(1);opacity:1}}

  .citation-tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
  .citation-tag{background:#ebf8ff;color:#2b6cb0;font-size:12px;padding:3px 10px;border-radius:12px}

  .input-area{padding:16px 32px 20px;background:#fff;border-top:1px solid #e2e8f0;display:flex;gap:12px;align-items:flex-end}
  .input-area textarea{flex:1;border:2px solid #e2e8f0;border-radius:12px;padding:12px 16px;font-size:15px;font-family:inherit;resize:none;outline:none;max-height:120px;line-height:1.5;transition:border-color .15s}
  .input-area textarea:focus{border-color:#4299e1}
  .input-area .send-btn{background:linear-gradient(135deg,#4299e1,#3182ce);color:#fff;border:none;border-radius:12px;padding:12px 24px;font-size:15px;font-weight:600;cursor:pointer;transition:all .15s;white-space:nowrap}
  .input-area .send-btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(66,153,225,.4)}
  .input-area .send-btn:disabled{opacity:.5;cursor:not-allowed;transform:none;box-shadow:none}

  ::-webkit-scrollbar{width:6px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#cbd5e0;border-radius:3px}
  ::-webkit-scrollbar-thumb:hover{background:#a0aec0}

  /* ====== 响应式 ====== */
  @media(max-width:768px){
    .sidebar{width:100%;max-height:40vh;border-right:none;border-bottom:1px solid #e2e8f0}
    .main{flex-direction:column}
    .messages{padding:16px}
    .input-area{padding:12px 16px}
    .msg-bubble{max-width:85%}
  }
</style>
</head>
<body>

<div class="header">
  <span class="icon">&#9878;</span>
  <h1>法律问答助手</h1>
  <span class="sub">上传法律文件，智能问答引用法条</span>
  <span class="badge">在线版</span>
</div>

<div class="main">
  <!-- 左侧 -->
  <div class="sidebar">
    <div class="sidebar-title">&#128196; 法律文件管理</div>
    <div class="upload-zone" id="uploadZone">
      <div class="ui">&#128228;</div>
      <p>点击或拖拽上传文件</p>
      <small>支持 .txt 和 .pdf</small>
      <input type="file" id="fileInput" accept=".txt,.pdf" multiple>
    </div>
    <div class="upload-status" id="uploadStatus"></div>
    <div class="doc-list" id="docList"><div class="doc-empty">暂无文件，请先上传</div></div>
  </div>

  <!-- 右侧 -->
  <div class="chat-area">
    <div class="messages" id="messages">
      <div class="welcome" id="welcome">
        <div class="wi">&#9878;</div>
        <h2>欢迎使用法律问答助手</h2>
        <p>先在左侧上传法律文件（如刑法、民法典等），<br>然后输入法律问题，助手将引用法条回答。</p>
        <div class="tips">
          <span class="tt" onclick="fill('什么是正当防卫？')">什么是正当防卫？</span>
          <span class="tt" onclick="fill('合同违约怎么赔偿？')">合同违约怎么赔偿？</span>
          <span class="tt" onclick="fill('离婚财产如何分割？')">离婚财产如何分割？</span>
          <span class="tt" onclick="fill('遗产继承的顺序是什么？')">遗产继承的顺序是什么？</span>
        </div>
      </div>
    </div>
    <div class="input-area">
      <textarea id="qInput" rows="1" placeholder="请输入您的法律问题..." onkeydown="hk(event)"></textarea>
      <button class="send-btn" id="sendBtn" onclick="send()">发送提问</button>
    </div>
  </div>
</div>

<script>
let busy=false;
window.addEventListener('DOMContentLoaded',()=>{loadDocs();ar()});

const fi=document.getElementById('fileInput'),uz=document.getElementById('uploadZone'),us=document.getElementById('uploadStatus');
fi.addEventListener('change',e=>{if(e.target.files.length)upAll(e.target.files)});
uz.addEventListener('dragover',e=>{e.preventDefault();uz.classList.add('dragover')});
uz.addEventListener('dragleave',()=>uz.classList.remove('dragover'));
uz.addEventListener('drop',e=>{e.preventDefault();uz.classList.remove('dragover');if(e.dataTransfer.files.length)upAll(e.dataTransfer.files)});

async function upAll(files){for(const f of files)await upOne(f);fi.value='';loadDocs()}
async function upOne(file){
  ss('loading','正在上传 "'+file.name+'"…');
  const fd=new FormData();fd.append('file',file);
  try{const r=await fetch('/api/upload',{method:'POST',body:fd});const d=await r.json();
    if(r.ok&&d.success)ss('success',d.message);else ss('error',d.error||'上传失败')}
  catch{ss('error','网络错误')}
}
function ss(t,m){us.className='upload-status '+t;us.textContent=m;if(t==='success')setTimeout(()=>us.className='upload-status',5000)}

async function loadDocs(){try{const r=await fetch('/api/documents');const d=await r.json();rdl(d.documents)}catch{}}
function rdl(docs){
  const el=document.getElementById('docList');
  if(!docs||!docs.length){el.innerHTML='<div class="doc-empty">暂无文件，请先上传</div>';return}
  el.innerHTML=docs.map(d=>`<div class="doc-item"><span class="di">${d.name.endsWith('.pdf')?'&#128211;':'&#128196;'}</span><div class="info"><div class="dn" title="${d.name}">${d.name}</div><div class="dm">${d.articles>0?d.articles+' 条法条':d.chunks+' 个段落'}</div></div><button class="dd" onclick="dd('${d.name}')" title="删除">&#10005;</button></div>`).join('')
}
async function dd(n){if(!confirm('确定删除 "'+n+'" 吗？'))return;try{await fetch('/api/documents/'+encodeURIComponent(n),{method:'DELETE'});loadDocs()}catch{alert('删除失败')}}

function fill(t){document.getElementById('qInput').value=t;document.getElementById('qInput').focus()}
function hk(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function ar(){const t=document.getElementById('qInput');t.addEventListener('input',()=>{t.style.height='auto';t.style.height=Math.min(t.scrollHeight,120)+'px'})}

async function send(){
  if(busy)return;const inp=document.getElementById('qInput'),q=inp.value.trim();if(!q)return;
  const w=document.getElementById('welcome');if(w)w.style.display='none';
  addMsg('user',q);inp.value='';inp.style.height='auto';
  busy=true;document.getElementById('sendBtn').disabled=true;const ld=addTyping();
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});const d=await r.json();
    ld.remove();if(d.answer)addMsg('bot',d.answer,d.citations);else if(d.error)addMsg('bot','错误：'+d.error)}
  catch{ld.remove();addMsg('bot','网络错误，请稍后再试。')}
  busy=false;document.getElementById('sendBtn').disabled=false
}

function addMsg(role,text,cites){
  const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row '+role;
  const av=role==='user'?'&#128100;':'&#9878;';
  let ch='';if(cites&&cites.length)ch='<div class="citation-tags">'+cites.map(c=>'<span class="citation-tag">'+esc(c.title+' ('+c.source+')')+'</span>').join('')+'</div>';
  r.innerHTML=(role==='user'?'':'<div class="msg-avatar">'+av+'</div>')+'<div class="msg-bubble">'+esc(text)+ch+'</div>'+(role==='user'?'<div class="msg-avatar">'+av+'</div>':'');
  c.appendChild(r);c.scrollTop=c.scrollHeight
}
function addTyping(){
  const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row bot';
  r.innerHTML='<div class="msg-avatar">&#9878;</div><div class="msg-bubble typing-indicator"><span></span><span></span><span></span></div>';
  c.appendChild(r);c.scrollTop=c.scrollHeight;return r
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
</script>
</body>
</html>'''


# ==================== 启动入口 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'

    print()
    print('=' * 50)
    print('    法律问答助手 Web 版已启动！')
    print('=' * 50)
    print(f'    本地访问: http://127.0.0.1:{port}')
    print(f'    局域网:   http://0.0.0.0:{port}')
    print('=' * 50)
    print()

    app.run(host='0.0.0.0', port=port, debug=debug)
