# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║     法律问答助手 v4.0 — 全量法律库 + DeepSeek AI     ║
║                                                      ║
║  内置法律库：1661部法律法规 + 两高司法解释            ║
║  AI 引擎：  DeepSeek 大模型智能回答                   ║
║  部署平台：Railway.com                                ║
╚══════════════════════════════════════════════════════╝
"""

import os
import re
import json
import gzip
import glob
import requests as http_requests
from flask import Flask, request, jsonify, render_template_string

try:
    import jieba
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, '-m', 'pip', 'install', 'jieba'])
    import jieba

# ==================== 配置 ====================
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 30 * 1024 * 1024

DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_MODEL = 'deepseek-v4-flash'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LAWS_DATA_FILE = os.path.join(BASE_DIR, 'laws_data.json.gz')
LAWS_DIR = os.path.join(BASE_DIR, 'laws')

# ==================== 文档管理器 ====================
class LegalDocManager:
    def __init__(self):
        self.documents = {}
        self.chunks = []
        self.law_count = 0
        self.category_count = 0

    def load_builtin_laws(self):
        """启动时加载全量法律数据"""
        categories = set()
        # 优先加载压缩数据文件（全量法律库）
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
        # 兼容：也加载 laws/ 目录下的 txt 文件
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
        """返回法律库统计信息"""
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
        # 提取问题中提到的法律名称
        law_names = set()
        for m in re.finditer(r'《(.+?)》', question):
            law_names.add(m.group(1))
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
            # 法律名称匹配加分
            if law_names:
                src = chunk['source']
                for ln in law_names:
                    if ln in src:
                        score += 30
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:top_n]]

    def search_only(self, question):
        """纯库搜索模式，不调用AI，不消耗tokens"""
        results = self.search(question, top_n=8)
        if not self.chunks:
            return {'answer': '法律库为空，请上传法律文件。', 'citations': [], 'ai_used': False}
        if not results:
            return {'answer': '抱歉，没有找到相关法律条文。建议换一种方式提问，或上传更多法律文件。',
                    'citations': [], 'ai_used': False}
        kw_str = '、'.join(self._keywords(question)[:6])
        header = f"找到以下 {len(results)} 条相关法条"
        if kw_str:
            header += f"（关键词：{kw_str}）"
        header += '：\n\n'
        citations = []
        display_parts = []
        for chunk in results:
            src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
            display = chunk['content'][:600] + ('……' if len(chunk['content']) > 600 else '')
            display_parts.append(f"【{chunk['title']}】（{src_display}）\n{display}")
            citations.append({'title': chunk['title'], 'source': src_display})
        return {'answer': header + '\n\n'.join(display_parts), 'citations': citations, 'ai_used': False}

    def answer_with_ai(self, question, history=None):
        """搜索法条 + DeepSeek 智能回答（支持上下文）"""
        results = self.search(question)

        if not self.chunks:
            return {'answer': '法律库为空，请上传法律文件。', 'citations': [], 'ai_used': False}

        if not results:
            if DEEPSEEK_API_KEY:
                ai_answer = self._call_deepseek(question, '', history)
                if ai_answer:
                    return {'answer': ai_answer, 'citations': [], 'ai_used': True}
            return {'answer': '抱歉，没有找到相关法律条文。建议换一种方式提问，或上传更多法律文件。',
                    'citations': [], 'ai_used': False}

        # 构建法条上下文
        context_parts = []
        citations = []
        for chunk in results:
            src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
            context_parts.append(f"【{chunk['title']}】（{src_display}）\n{chunk['content']}")
            citations.append({'title': chunk['title'], 'source': src_display})
        context = '\n\n'.join(context_parts)

        # 调用 DeepSeek
        if DEEPSEEK_API_KEY:
            ai_answer = self._call_deepseek(question, context, history)
            if ai_answer:
                return {'answer': ai_answer, 'citations': citations, 'ai_used': True}

        # AI 不可用时的降级方案
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
        return {'answer': header + '\n\n'.join(display_parts), 'citations': citations, 'ai_used': False}

    def _call_deepseek(self, question, context, history=None):
        """调用 DeepSeek API（支持多轮对话上下文）"""
        system_prompt = (
            '你是一位专业的中国法律顾问，名叫"法小智"。你拥有涵盖宪法、民法典、刑法、行政法、经济法、'
            '社会法、诉讼法、司法解释等1600余部法律法规的完整知识库。请根据提供的法律条文回答用户的问题。\n\n'
            '回答要求：\n'
            '1. 准确引用具体法条（如"根据《民法典》第577条"）\n'
            '2. 用通俗易懂的语言解释法律含义\n'
            '3. 如果涉及多个法条，分点说明\n'
            '4. 如果提供的法条不足以完整回答，请如实说明并给出一般性法律建议\n'
            '5. 回答末尾提醒用户：具体案件建议咨询专业律师\n'
            '6. 不要编造不存在的法条\n'
            '7. 结合之前的对话上下文理解用户的追问，保持回答连贯'
        )
        if context:
            user_msg = f'以下是相关法律条文：\n\n{context}\n\n---\n用户问题：{question}'
        else:
            user_msg = f'用户问题：{question}\n\n（未找到直接相关的法条，请根据你的法律知识回答，并注明仅供参考）'
        messages = [{'role': 'system', 'content': system_prompt}]
        # 加入历史对话（最多保留最近6轮，控制token消耗）
        if history:
            for h in history[-12:]:
                if h.get('role') in ('user', 'assistant') and h.get('content'):
                    messages.append({'role': h['role'], 'content': h['content']})
        messages.append({'role': 'user', 'content': user_msg})
        try:
            resp = http_requests.post(
                DEEPSEEK_URL,
                headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {DEEPSEEK_API_KEY}'},
                json={
                    'model': DEEPSEEK_MODEL,
                    'messages': messages,
                    'temperature': 0.3,
                    'max_tokens': 2000,
                },
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

@app.route('/api/upload', methods=['POST'])
def upload():
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
        return jsonify(error='请输入问题'), 400
    question = data['question'].strip()
    mode = data.get('mode', 'ai')
    history = data.get('history', [])
    if mode == 'search':
        result = dm.search_only(question)
    else:
        result = dm.answer_with_ai(question, history)
    return jsonify(result)

@app.route('/api/article', methods=['GET'])
def get_article():
    source = request.args.get('source', '')
    title = request.args.get('title', '')
    if not source or not title:
        return jsonify(error='缺少参数'), 400
    # 在chunks中查找匹配的法条
    for chunk in dm.chunks:
        src_display = chunk['source'].split('/')[-1] if '/' in chunk['source'] else chunk['source']
        if chunk['title'] == title and (src_display == source or source in chunk['source']):
            return jsonify(found=True, title=chunk['title'], source=src_display, content=chunk['content'])
    return jsonify(found=False, error='未找到该条文')

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

# ==================== 前端 ====================
HTML_TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>法律问答助手 - 全量法律库 AI 版</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;background:#eef2f7;color:#333;height:100vh;display:flex;flex-direction:column;overflow:hidden}
  .header{background:linear-gradient(135deg,#1a365d,#2c5282);color:#fff;padding:14px 28px;display:flex;align-items:center;gap:12px;flex-shrink:0;box-shadow:0 2px 8px rgba(0,0,0,.15)}
  .header .icon{font-size:28px}
  .header h1{font-size:22px;font-weight:600;letter-spacing:2px}
  .header .sub{font-size:13px;opacity:.7;margin-left:8px}
  .header .badge{margin-left:auto;background:rgba(255,255,255,.15);padding:4px 14px;border-radius:20px;font-size:12px}
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
  .doc-item .tag{font-size:10px;background:#ebf8ff;color:#2b6cb0;padding:1px 6px;border-radius:8px;margin-left:4px}
  .doc-item .dd{background:none;border:none;font-size:16px;color:#e53e3e;cursor:pointer;padding:4px 8px;border-radius:6px}
  .doc-item .dd:hover{background:#fff5f5}
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
  .citation-tag{background:#ebf8ff;color:#2b6cb0;font-size:11px;padding:3px 10px;border-radius:12px;cursor:pointer;transition:all .15s;border:1px solid transparent}
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
  .mode-toggle .mt-hint{font-size:11px;color:#a0aec0;margin-left:4px}
  ::-webkit-scrollbar{width:6px}
  ::-webkit-scrollbar-track{background:transparent}
  ::-webkit-scrollbar-thumb{background:#cbd5e0;border-radius:3px}
  @media(max-width:768px){.sidebar{width:100%;max-height:35vh;border-right:none;border-bottom:1px solid #e2e8f0}.main{flex-direction:column}.messages{padding:16px}.input-area{padding:12px 16px}.msg-bubble{max-width:88%}}
</style>
</head>
<body>
<div class="header">
  <span class="icon">&#9878;</span>
  <h1>法律问答助手</h1>
  <span class="sub">DeepSeek AI · 1661部法律法规 + 司法解释</span>
  <span class="badge">全量法律库 v4.0</span>
</div>
<div class="main">
  <div class="sidebar">
    <div class="sidebar-title">&#128218; 法律知识库</div>
    <div class="stats-bar" id="statsBar">正在加载法律库统计…</div>
    <div class="upload-zone" id="uploadZone">
      <div class="ui">&#128228;</div>
      <p>上传更多法律文件</p>
      <small>.txt / .pdf</small>
      <input type="file" id="fileInput" accept=".txt,.pdf" multiple>
    </div>
    <div class="upload-status" id="uploadStatus"></div>
    <div class="doc-list" id="docList"></div>
    <div class="flk-link">
      <a href="https://flk.npc.gov.cn/" target="_blank">&#128279; 在国家法律法规数据库中搜索</a>
    </div>
  </div>
  <div class="chat-area">
    <div class="messages" id="messages">
      <div class="welcome" id="welcome">
        <div class="wi">&#9878;</div>
        <h2>你好，我是法小智</h2>
        <p>我已内置 <b>1661部</b> 法律法规及两高司法解释，涵盖宪法、民法典、刑法、行政法、经济法、社会法、诉讼法等全部法律门类，由 DeepSeek AI 驱动。<br>直接输入法律问题即可获得智能解答。</p>
        <div class="tips">
          <span class="tt" onclick="fill('什么是正当防卫？')">什么是正当防卫？</span>
          <span class="tt" onclick="fill('合同违约怎么赔偿？')">合同违约怎么赔偿？</span>
          <span class="tt" onclick="fill('离婚财产如何分割？')">离婚财产如何分割？</span>
          <span class="tt" onclick="fill('遗产继承的顺序是什么？')">遗产继承的顺序？</span>
          <span class="tt" onclick="fill('劳动者被辞退有什么补偿？')">被辞退有什么补偿？</span>
          <span class="tt" onclick="fill('交通肇事罪怎么量刑？')">交通肇事罪量刑？</span>
        </div>
      </div>
    </div>
    <div class="input-area">
      <div style="flex:1;display:flex;flex-direction:column">
        <div class="mode-toggle">
          <span class="mt-label active" id="modeAi" onclick="setMode('ai')">&#129302; AI智能回答</span>
          <span class="mt-label" id="modeSearch" onclick="setMode('search')">&#128269; 直接搜索</span>
          <span class="mt-hint" id="modeHint">AI回答更精准，消耗tokens</span>
        </div>
        <textarea id="qInput" rows="1" placeholder="请输入您的法律问题..." onkeydown="hk(event)"></textarea>
      </div>
      <button class="send-btn" id="sendBtn" onclick="send()">发送提问</button>
    </div>
  </div>
</div>
<script>
let busy=false;
let chatMode='ai';
let abortCtrl=null;
let chatHistory=[];
function setMode(m){
  chatMode=m;
  const ai=document.getElementById('modeAi'),se=document.getElementById('modeSearch'),hint=document.getElementById('modeHint');
  if(m==='ai'){ai.className='mt-label active';se.className='mt-label';hint.textContent='AI回答更精准，消耗tokens'}
  else{ai.className='mt-label';se.className='mt-label active-search';hint.textContent='直接搜索法律库，免费不消耗tokens'}
}
function setBtnStop(isStop){
  const b=document.getElementById('sendBtn');
  if(isStop){b.textContent='⏹ 停止';b.className='send-btn stop';b.onclick=cancelSend}
  else{b.textContent='发送提问';b.className='send-btn';b.onclick=send}
}
function cancelSend(){
  if(abortCtrl){abortCtrl.abort();abortCtrl=null}
}
window.addEventListener('DOMContentLoaded',()=>{loadDocs();loadStats();ar()});
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
async function loadStats(){
  try{const r=await fetch('/api/stats');const d=await r.json();
    const cats=Object.entries(d.categories||{}).sort((a,b)=>b[1]-a[1]).slice(0,6).map(e=>e[0]).join('、');
    document.getElementById('statsBar').innerHTML='<b>'+d.total+'</b> 部法律法规 · <b>'+d.chunks.toLocaleString()+'</b> 个条文<br>涵盖：'+cats+' 等';
  }catch{document.getElementById('statsBar').textContent='法律库已就绪'}
}
async function loadDocs(){try{const r=await fetch('/api/documents');const d=await r.json();rdl(d.documents)}catch{}}
function rdl(docs){
  const el=document.getElementById('docList');
  const uploads=docs.filter(d=>!d.builtin);
  if(!uploads.length){el.innerHTML='<div style="text-align:center;padding:16px;color:#a0aec0;font-size:13px">内置法律库已包含全部法律法规<br>如需补充可上传文件</div>';return}
  el.innerHTML='<div style="padding:4px 0 8px;font-size:12px;color:#718096">用户上传 ('+uploads.length+')</div>'+uploads.map(d=>{
    const icon=d.name.endsWith('.pdf')?'&#128211;':'&#128196;';
    const displayName=d.name.replace(/^upload_/,'');
    return `<div class="doc-item"><span class="di">${icon}</span><div class="info"><div class="dn" title="${displayName}">${displayName}</div><div class="dm">${d.articles>0?d.articles+' 条法条':d.chunks+' 个段落'}</div></div><button class="dd" onclick="dd('${d.name}')" title="删除">&#10005;</button></div>`
  }).join('')
}
async function dd(n){if(!confirm('确定删除吗？'))return;try{await fetch('/api/documents/'+encodeURIComponent(n),{method:'DELETE'});loadDocs()}catch{alert('删除失败')}}
function fill(t){document.getElementById('qInput').value=t;document.getElementById('qInput').focus()}
function hk(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function ar(){const t=document.getElementById('qInput');t.addEventListener('input',()=>{t.style.height='auto';t.style.height=Math.min(t.scrollHeight,120)+'px'})}
async function send(){
  if(busy)return;const inp=document.getElementById('qInput'),q=inp.value.trim();if(!q)return;
  const w=document.getElementById('welcome');if(w)w.style.display='none';
  addMsg('user',q);inp.value='';inp.style.height='auto';
  chatHistory.push({role:'user',content:q});
  busy=true;setBtnStop(true);abortCtrl=new AbortController();const ld=addTyping();
  try{const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q,mode:chatMode,history:chatHistory.slice(0,-1)}),signal:abortCtrl.signal});const d=await r.json();
    ld.remove();
    if(d.answer){addMsg('bot',d.answer,d.citations,d.ai_used);chatHistory.push({role:'assistant',content:d.answer})}
    else if(d.error)addMsg('bot','错误：'+d.error)}
  catch(e){ld.remove();if(e.name==='AbortError'){addMsg('bot','⏹ 已取消提问，未消耗tokens。',[],false);chatHistory.pop()}else addMsg('bot','网络错误，请稍后再试。')}
  busy=false;abortCtrl=null;setBtnStop(false)
}
function addMsg(role,text,cites,aiUsed){
  const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row '+role;
  const av=role==='user'?'&#128100;':'&#9878;';
  let aiBadge=(aiUsed&&role==='bot')?'<div class="ai-badge">&#129302; DeepSeek AI 回答</div>':'';
  let ch='';if(cites&&cites.length)ch='<div class="citation-tags">'+cites.map(c=>'<span class="citation-tag" onclick="showArticle(\''+esc(c.title).replace(/'/g,"\\'")+'\',\''+esc(c.source).replace(/'/g,"\\'")+'\')" title="点击查看原文">'+esc(c.title+' · '+c.source.replace(/^upload_/,''))+'</span>').join('')+'</div>';
  r.innerHTML=(role==='user'?'':'<div class="msg-avatar">'+av+'</div>')+'<div class="msg-bubble">'+aiBadge+esc(text)+ch+'</div>'+(role==='user'?'<div class="msg-avatar">'+av+'</div>':'');
  c.appendChild(r);c.scrollTop=c.scrollHeight
}
function addTyping(){
  const c=document.getElementById('messages'),r=document.createElement('div');r.className='msg-row bot';
  r.innerHTML='<div class="msg-avatar">&#9878;</div><div class="msg-bubble typing-indicator"><span></span><span></span><span></span></div>';
  c.appendChild(r);c.scrollTop=c.scrollHeight;return r
}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function showArticle(title,source){
  if(busy)return;
  busy=true;
  const ld=addTyping();
  try{
    const r=await fetch('/api/article?title='+encodeURIComponent(title)+'&source='+encodeURIComponent(source));
    const d=await r.json();
    ld.remove();
    if(d.found){
      addMsg('bot','📜 '+d.title+'（'+d.source+'）\n\n'+d.content,[],false);
    }else{
      addMsg('bot','抱歉，未能找到该条文原文。',[],false);
    }
  }catch{ld.remove();addMsg('bot','网络错误，请稍后再试。',[],false)}
  busy=false;
}
</script>
</body>
</html>'''

# ==================== 启动 ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print()
    print('=' * 50)
    print('    法律问答助手 v4.0 (全量法律库 + DeepSeek AI)')
    print(f'    内置法律: {dm.law_count} 部, {len(dm.chunks)} 个条文')
    print(f'    AI 引擎:  {"DeepSeek" if DEEPSEEK_API_KEY else "未配置"}')
    print(f'    访问:     http://127.0.0.1:{port}')
    print('=' * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
