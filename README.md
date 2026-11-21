# Xudaai Product Library

本项目是一个可迁移的浏览器版产品资料检索系统：同一套 Docker Compose 可运行在开发电脑或支持 Docker Compose 的 NAS。网页、钉钉适配器和未来的 MCP 都调用统一的检索 API；迁移时修改 `.env` 中的数据目录和资料目录挂载路径。

## 当前版本

- 本地网页：资料搜索、可选模型问答、以图搜图、资料上传和索引管理。
- AI 对话：同一对话框支持文字问题和图片；图片先在本地用 OpenCLIP 找相似产品图，再取同目录文档与文字检索结果作为依据，并显示引用来源。配置支持视觉输入的模型后，可把图片和命中片段一并交给模型回答；未配置模型时仍可使用本地检索。
- 增量索引：新建、修改、删除文件会在下次同步时更新；默认每 5 分钟扫描一次，也可在网页手动同步；未变更文件跳过。
- 文档解析：PDF、DOCX、XLSX、PPTX、TXT、Markdown、CSV。
- 图片索引：支持 pHash 近重复检索；设置 `IMAGE_SEARCH_MODE=clip` 并以 `ENABLE_CLIP=1` 构建后，使用 OpenCLIP 做语义/视觉相似检索。CLIP 首次使用会下载模型并消耗较多内存和存储，需确认 NAS CPU 架构有可用的 PyTorch wheel。
- API：`/api/assistant/chat`（文字/图片对话）、`/api/search/text`、`/api/search/image`、`/api/index/sync`、`/api/files/{id}/preview`。
- 钉钉：使用官方 `dingtalk-stream` SDK 的 Stream 长连接机器人，配置企业内部应用 Client ID/Secret 即可启用；无需暴露公网回调。机器人复用统一检索和模型回答。
- 对话内模型管理：可新增、修改、删除多个硅基流动、DeepSeek、OpenAI、OpenAI 兼容或 Ollama 配置；每轮对话可选择指定模型，也可选择仅本地检索。密钥保存在 `data/settings.json`，页面不回显。
- 可选媒体处理：启用 `ENABLE_MEDIA_AI` 后，扫描件 PDF/图片可做 OCR，音视频可用 Faster Whisper 转写并按时间点检索。

## 本地开发

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Optional, for CLIP semantic image search:
pip install -r requirements-clip.txt
# Optional, for scanned-document OCR and audio/video transcription:
pip install -r requirements-media.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>。把产品资料复制到 `library/`，在“资料同步”页面点击“立即同步”，或使用网页上传。直接运行 Python 时，资料目录通过 `LIBRARY_DIR` 设置，索引和数据库目录通过 `DATA_DIR` 设置。

### Windows 本机使用（不依赖 Docker）

1. 在 `.env` 中将 `LIBRARY_DIR` 设置为资料目录，例如 `C:/Users/jojo/Desktop/Yupoo下载`。
2. 首次安装依赖时运行 `python -m venv .venv`、`.\.venv\Scripts\python.exe -m pip install -r requirements.txt`，语义以图搜图再运行 `.\.venv\Scripts\python.exe -m pip install -r requirements-clip.txt`。
3. 双击 `start-local.bat` 启动网页服务，再访问 <http://127.0.0.1:8000>。首次启动会下载 CLIP 模型并建立索引；后续新增或修改资料会自动同步。

首次索引大量图片时会持续占用 CPU 和约 1–2 GB 内存，完成后保持服务运行即可每 5 分钟检查更新。关闭启动窗口会停止服务。

## Docker Compose

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
docker compose up -d --build
```

默认网页端口为 `8000`。`.env` 中 `LIBRARY_HOST_PATH` 指向原始资料目录，`APP_DATA_HOST_PATH` 指向数据库/索引目录，`APP_PORT` 设置网页端口。NAS 上把这两个值改为 NAS 上实际目录（例如 `/volume1/docker/xudaai/library`、`/volume1/docker/xudaai/data`），再将项目 Compose 文件部署到 NAS。应用内路径保持 `/srv/app/library` 与 `/srv/app/data` 不变。资料目录需允许应用容器读写（网页上传功能需要写权限）。迁移时保留配置、数据库和资料目录；也可以复制原始资料后执行一次同步重建索引。

首次部署前设置 `.env` 中的 `APP_PASSWORD`。OpenCLIP 语义图搜默认开启；不支持 PyTorch 的 NAS 可将 `ENABLE_CLIP=0`、`IMAGE_SEARCH_MODE=phash`。如需 OCR/音视频转写，设 `ENABLE_MEDIA_AI=1` 后重新构建，首次转写会下载 Whisper 模型。模型设置页可直接选择 DeepSeek 并填入 API Key，无需改代码；API Key 会保存在映射到 NAS 的 `data/settings.json` 中，迁移时应妥善保护该文件。

## 资料维护机制

每个文件记录相对路径、大小、修改时间、SHA-256、状态和索引时间。同步使用 SHA-256 识别实际内容变化：新增/变化的文件重新解析，删除文件从索引移除，未变化文件跳过。解析失败保留错误信息，可修复源文件后再次同步。

网页管理页支持手动增量同步、上传资料和状态查看；默认每 300 秒自动扫描一次（`SYNC_INTERVAL_SECONDS=0` 可关闭）。索引目录应定期备份；原始文件仍是权威数据源。

## 报价、PI 与运费工作流

AI 对话页提供“生成产品报价”和“生成 PI 形式发票”快捷入口。可勾选关键词检索结果或图片检索结果中的多个产品；单据卡片按产品、客户、贸易条款三步操作。选中资料可一键导入，或直接手工添加产品行。客户可保存为常用客户；币种、有效期、贸易条款、付款方式、运费/税费等为可选项并支持自定义。颜色、尺码配比、数量和 MOQ 可逐项填写。点击“导入已选产品”时，最近用户消息和所选资料摘录会发送给当前选择的已配置模型；不会要求通过多轮文字确认才可生成。产品单价由用户填写和核对；金额在服务端计算。报价/PI 生成后锁定为只读版本，草稿和 PDF 保存在 `DATA_DIR`。

DOCX 模板可放在 `data/templates/`，也可从 AI 对话中的报价/PI 单据窗口上传；可在 `data/templates/quote_defaults.json` 配置公司抬头、常用条款、默认币种和模板，或在页面点“保存默认配置”。常用客户保存在 `data/templates/customers.json`。模板占位符示例：`{{quote_no}}`、`{{customer}}`、`{{items}}`、`{{total}}`、`{{payment_terms}}`、`{{bank_details}}`。DOCX 的段落和表格文字会用于 PDF 内容；当前转换器会用应用的标准 PDF 样式重排，复杂 Word 样式、页眉页脚和内嵌图片不保证原样保留。

Windows 本机 PDF 使用微软雅黑；Docker 镜像安装 Noto CJK 字体。若需要自定义字体，可设置 `QUOTE_FONT_PATH` 指向可用的中文 TrueType Collection 字体。当前版本支持手工录入价格和运费；价目表导入、自动运费规则和 PI 模板属于后续功能，接入前需提供实际工作簿及其计费说明。公司抬头、联系信息、付款条款在每张报价单中填写。

## API 示例

```powershell
curl.exe -X POST -F "q=产品的安装方式" http://127.0.0.1:8000/api/search/text
curl.exe -X POST -F "image=@sample.jpg" http://127.0.0.1:8000/api/search/image
curl.exe -X POST http://127.0.0.1:8000/api/index/sync
```

## 钉钉设置

1. 在钉钉开放平台创建企业内部应用，启用机器人并选择 Stream 模式。
2. 在 `.env` 配置 `DINGTALK_ENABLED=true`、`DINGTALK_CLIENT_ID` 和 `DINGTALK_CLIENT_SECRET`。
3. `docker compose up -d --build` 后，机器人通过出站长连接接收消息；家庭网络无需设置入站端口映射。
4. 在模型设置页选择 DeepSeek 等兼容服务，或使用 Ollama；钉钉机器人的问答会复用该设置。没有模型时退化为资料检索结果。

## 安全与能力边界

- 文本检索使用 SQLite FTS；中文查询支持相邻字符回退检索。模型回答基于命中片段并返回来源。ChatGPT Plus/Pro 订阅不能代替模型 API Key；可在模型设置页接 DeepSeek 等兼容 API，或接本地 Ollama。
- OpenCLIP 语义图搜需 `ENABLE_CLIP=1`，首次索引会下载模型。视频检索以音轨转写为主，不会自动理解整段视频画面。
- 设置 `APP_PASSWORD` 可启用浏览器/API 的 HTTP Basic 登录。未设置密码时只限可信局域网使用，不要直接暴露公网。请给 NAS 配强密码并限制访问网段。
- 钉钉机器人通过 Stream 出站连接运行；配置应用凭据后可在内部团队使用。回传图片预览给钉钉的能力还需配置可访问的 HTTPS 文件地址。
- ChatGPT MCP 连接需要公网 HTTPS 与单独的访问授权；当前版本集中完成浏览器和钉钉入口，MCP 远程入口需后续单独启用，不能直接把本机地址交给 ChatGPT 云端访问。

## 迁移到 NAS

1. 在台式机用 Docker Compose 运行并确认资料、搜索、索引更新流程。
2. 把整个项目配置、原始资料目录和 `data/` 复制到 NAS；不要只复制数据库而遗漏原件。
3. 在 NAS 修改 `.env`：`LIBRARY_HOST_PATH`、`APP_DATA_HOST_PATH`、`MODEL_CACHE_HOST_PATH` 指向 NAS 实际目录；`APP_PORT` 选择空闲端口。
4. NAS 如支持对应 PyTorch 架构，可保留 `ENABLE_CLIP=1` 和 `IMAGE_SEARCH_MODE=clip`；否则设置 `ENABLE_CLIP=0`、`IMAGE_SEARCH_MODE=phash`。
5. 在 NAS 项目目录启动 `docker compose up -d --build`，之后局域网设备通过 `http://NAS地址:端口` 使用。
6. 避免同时在台式机和 NAS 对同一 SQLite 数据目录写入。迁移完成并验证后再停掉旧实例。

H99 Pro 目前不适合作为容器运行主机；若它仅提供 SMB 共享，可存原始文件，但服务需要部署在台式机或支持 Docker Compose 的 NAS 上。
