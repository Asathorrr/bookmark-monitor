# 📌 书签更新监控系统 · Bookmark Monitor

> 本地运行的 Chrome 书签内容变更追踪工具，基于 Python + Flask，支持自定义 CSS 选择器精准监控、Cookie 登录态保持、Cloudflare 绕过，以及 Windows 开机自启。

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-2.x-black?logo=flask)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| 📂 **书签树展示** | 自动读取 Chrome 书签，支持文件夹展开/折叠与关键词搜索 |
| ➕ **一键添加监控** | 点击书签旁的「+ 监控」按钮即可添加，支持整页或指定 CSS 选择器 |
| 🔄 **内容变更检测** | 对比页面内容哈希，有更新时自动高亮显示 |
| 🍪 **Cookie 支持** | 可配置各域名的登录 Cookie，支持需登录的页面监控 |
| 🛡️ **Cloudflare 绕过** | 安装 `curl_cffi` 后自动使用浏览器指纹模式请求 |
| 📁 **批量添加** | 支持整个书签文件夹批量添加到监控列表 |
| 🕐 **开机自启** | 提供 Windows 任务计划程序安装脚本 |
| 🌐 **跨平台** | 自动识别 Windows / macOS / Linux 的 Chrome 书签路径 |

---

## 📁 项目结构

```
bookmark-monitor/
├── app.py                  # Flask 后端（主程序）
├── templates/
│   └── index.html          # 前端单页应用
├── autostart/
│   ├── install.bat         # Windows 开机自启安装脚本
│   ├── uninstall.bat       # 卸载开机自启脚本
│   └── bookmark_monitor.xml # 任务计划模板
├── fix_config.py           # 配置文件修复工具
├── .gitignore
└── README.md
```

> `config.json` 和 `bookmark_snapshot.json` 在运行时自动生成，包含本地数据，已加入 `.gitignore`。

---

## ⚙️ 安装

### 1. 克隆仓库

```bash
git clone https://github.com/YOUR_USERNAME/bookmark-monitor.git
cd bookmark-monitor
```

### 2. 安装依赖

**基础依赖（必须）：**

```bash
pip install flask flask-cors requests beautifulsoup4
```

**可选：Cloudflare 绕过支持**

```bash
pip install curl_cffi
```

> 安装 `curl_cffi` 后，程序会自动使用浏览器指纹模式发送请求，可绕过大多数 Cloudflare 防护。

---

## 🚀 运行

```bash
python app.py
```

启动后在浏览器访问：**http://localhost:5000**

---

## 🖥️ 界面说明

程序采用左右双栏布局：

```
┌─────────────────────┬─────────────────────────┐
│   Chrome 书签树      │     监控列表             │
│                     │                         │
│  📁 书签栏           │  [一键检查更新]           │
│  ├─ 📌 网站A  [+监控]│                         │
│  ├─ 📌 网站B  [+监控]│  🔴 网站A（有更新）       │
│  └─ 📁 子文件夹      │  ✅ 网站B（无变化）       │
│     └─ 📌 网站C      │                         │
└─────────────────────┴─────────────────────────┘
```

---

## 📖 使用教程

### 添加监控

1. 在左侧「Chrome 书签」区域浏览书签树
2. 鼠标悬停在书签条目上，点击右侧出现的 **「+ 监控」** 按钮
3. 在弹出窗口中：
   - **CSS 选择器**（可留空）：留空则监控整个页面，填写则只监控指定元素
   - **Cookie**：如需访问登录后才可见的内容，在此粘贴 Cookie 字符串
4. 点击「确认添加」

### 批量添加文件夹

鼠标悬停在文件夹上，点击右侧出现的 **「批量添加」** 按钮，即可将该文件夹下所有书签一次性加入监控列表。

### 检查更新

点击右侧面板顶部的 **「一键检查更新」** 按钮，后端并发抓取所有监控 URL 并对比内容。

- **红色卡片** = 检测到内容变化
- **绿色卡片** = 内容无变化
- 点击红色卡片上的 **「✓ 标为已读」** 可重置更新状态

### 管理 Cookie

1. 点击右上角 **「Cookie 管理」** 按钮
2. 填写域名（如 `example.com`）和对应的 Cookie 字符串
3. Cookie 按域名存储，会自动匹配同域下的所有监控 URL

---

## 💡 CSS 选择器获取方法

精准监控页面特定区域（如文章列表、价格、公告），需要提供 CSS 选择器：

1. 在目标网页按 `F12` 打开 DevTools
2. 使用元素选择器（左上角箭头图标）点击目标元素
3. 在 Elements 面板中右键该元素 → **复制** → **复制 selector**
4. 粘贴到添加监控时的「CSS 选择器」输入框

**示例：**

| 用途 | 选择器示例 |
|------|------------|
| 文章列表 | `#article-list` |
| 商品价格 | `.product-price span` |
| 公告栏 | `div.notice-board` |
| 留空 | 监控整个页面 |

---

## 🕐 Windows 开机自启

让程序随 Windows 登录自动后台启动，无需每次手动运行：

### 安装

```
以管理员身份运行 autostart\install.bat
```

安装完成后：
- 程序会在每次登录时自动后台启动
- 浏览器访问 http://localhost:5000 即可使用

### 卸载

```
运行 autostart\uninstall.bat
```

---

## 🌐 Chrome 书签路径参考

代码会自动检测当前操作系统并读取对应路径，**无需手动配置**：

| 系统 | 路径 |
|------|------|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data\Default\Bookmarks` |
| macOS | `~/Library/Application Support/Google/Chrome/Default/Bookmarks` |
| Linux | `~/.config/google-chrome/Default/Bookmarks` |

---

## 🔧 常见问题

**Q: 书签不显示？**
A: 确认 Chrome 已安装且有书签。如路径不对，检查是否使用了 Chrome 的其他 Profile（非 Default）。

**Q: 某些网站检测不到变化？**
A: 部分网站内容通过 JS 动态渲染，静态抓取无法获取。可尝试指定静态部分的 CSS 选择器。

**Q: 提示 Cloudflare 被拦截？**
A: 安装 `curl_cffi`：`pip install curl_cffi`，重启程序后会自动使用浏览器指纹模式。

**Q: 需要登录的网站无法监控？**
A: 在添加监控时填入该网站的 Cookie，或在「Cookie 管理」页面按域名配置。

**Q: config.json 损坏无法启动？**
A: 运行 `python fix_config.py` 进行修复。

---

## 📦 依赖说明

| 包 | 用途 | 是否必须 |
|----|------|----------|
| `flask` | Web 框架 | ✅ |
| `flask-cors` | 跨域支持 | ✅ |
| `requests` | HTTP 请求 | ✅ |
| `beautifulsoup4` | HTML 解析 | ✅ |
| `curl_cffi` | 浏览器指纹 / Cloudflare 绕过 | 可选 |

---

## 📄 License

MIT License — 自由使用、修改和分发。
