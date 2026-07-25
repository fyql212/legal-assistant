# 法律问答助手 - Web 云端版

上传法律文件（TXT/PDF），智能问答并引用法条。

## 本地运行

```bash
pip install -r requirements.txt
python app.py
```

然后打开 http://127.0.0.1:5000

## 部署到 Render.com

### 第一步：注册 GitHub 账号

访问 https://github.com 注册（如已有可跳过）。

### 第二步：创建仓库并上传代码

1. 登录 GitHub，点击右上角 "+" → "New repository"
2. 仓库名填 `legal-assistant`，选 Public，点 Create repository
3. 在仓库页面点 "uploading an existing file"
4. 拖拽以下 5 个文件进去，点 Commit changes：
   - `app.py`
   - `requirements.txt`
   - `Procfile`
   - `runtime.txt`
   - `.gitignore`

### 第三步：注册 Render 并部署

1. 访问 https://render.com 用 GitHub 账号登录
2. 点 "New +" → "Web Service"
3. 连接你的 GitHub 仓库 `legal-assistant`
4. 配置页面保持默认即可（Free 计划）
5. 点 "Create Web Service"

等待 2-3 分钟部署完成，Render 会给你一个公网链接（如 `https://legal-assistant-xxxx.onrender.com`），把这个链接发给任何人，他们打开浏览器就能用！

## 注意事项

- Render 免费版在 15 分钟无访问后会自动休眠，下次访问需等待 30 秒唤醒
- 上传的法律文件存在内存中，服务重启后需重新上传
- 如需持久存储，可升级为付费方案或接入数据库
