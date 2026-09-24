# 飞书“已投递岗位”收件箱

`scripts/feishu_application_inbox.py` 是一个双向飞书自建应用机器人收件端。用户私聊机器人发送岗位 URL、JD 截图，或两者同时发送，即视为本人已经投递；系统不会访问登录态招聘平台，也不会点击申请按钮。

## 当前实现

- 处理 `im.message.receive_v1` 私聊事件，事件 ID 和消息 ID 幂等；HTTP 回调必须配置事件 Verification Token，未配置或校验失败会拒绝请求。
- URL 优先抓取公开岗位详情；BOSS/App 等受限链接只保存用户证据，不绕过登录、验证码或反爬。公开招聘站 URL 只进入来源证据，不自动填入 `Official URL`；只有已知官方 ATS 或可确认的公司域名才会填入。
- 图片原件保存到 `data/job_cache/feishu_inbox_evidence/`；有 `tesseract` 时尝试本地 OCR，没有 OCR 时进入 `NEEDS_INFO`，不会静默丢弃。
- 从 URL、截图 OCR 或补充文字中提取公司、职位、地点；缺公司/职位时只追问缺失字段。
- 默认 `Applied At` 是消息发送时间，并在回复中明确提示；截图拍摄时间不会被当成投递时间。
- 用规范 URL 优先、再用公司+职位+地点匹配现有岗位；重复消息和重发截图不创建重复记录。
- 已有岗位（包括 WATCH）更新为 `Stage=APPLIED`；新岗位进入同一岗位池。无 URL 的证据保持 `Official URL` 为空、验证状态不确定，不伪造 OPEN/READY。
- “我不投”使用 `Stage=WITHDRAWN` 和 `Action=REJECT` 表达用户主动退出，`Job Status` 保持原值；这不是雇主拒绝。
- 本地应用收件状态和已投递岗位会写入 `data/job_cache/feishu_application_inbox.json` 与 `feishu_application_jobs.json`，日同步会把它作为人工来源合并。
- 生产 `--apply-feishu` 日跑会先只读导出 `feishu_human_state.json`，在评分前覆盖 `Applied At`、`Stage`、`Action` 和跟进字段；空字段不会被写成默认值。隔离验收不访问 Feishu。
- 长连接入口使用官方 `lark-oapi` WebSocket 客户端，运行参数为 `--long-connection`；启动时会自动扫描并恢复状态库中带有 `raw_event` 的 `PROCESSING` 事件。SDK 负责心跳和重连，外层也会在客户端退出后重启连接。

## 本地回放

```bash
python3 scripts/feishu_application_inbox.py \
  --config config/feishu.yaml \
  --input tests/fixtures/feishu_application_event.json
```

## 常驻接收端

优先使用本轮已选的长连接方式：

```powershell
& D:\Python\python.exe scripts\feishu_application_inbox.py `
  --config config\feishu.yaml --long-connection
```

必须在 `receiver.allowed_open_ids` 填入允许私聊机器人的 Feishu `open_id`；空名单会拒绝所有实时消息。安装依赖后才可启动：`pip install -r requirements.txt`。长连接不需要公网回调 URL 或 Verification Token。

旧的 HTTP 接收端仍可用于已有反向代理，但不是本轮实际运行路径：

```bash
python3 scripts/feishu_application_inbox.py \
  --config config/feishu.yaml \
  --host 127.0.0.1 --port 8787 --serve
```

HTTP 生产环境必须把 Feishu 开放平台事件回调 URL 通过现有 HTTPS 反向代理转发到该端口，并配置 `FEISHU_EVENT_VERIFICATION_TOKEN`。收件器先把原始事件和幂等键原子写入本地状态，再确认 HTTP 接收；处理失败会留下 `PROCESSING/NEEDS_INFO` 待办，重启或重投可恢复。本地 Windows 定时任务不适合作为即时收件器；电脑关机期间要可靠收件，需要一台持续运行的 HTTPS 主机或组织已有的云/内网网关。本项目不擅自开通付费云服务。

机器人应用至少需要由管理员核对并发布：事件 `im.message.receive_v1`；读取消息中的资源文件（收件截图下载使用 `GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=image`）；回复消息；读取/写入目标多维表。当前用户已决定不新增截图附件字段，因此不需要附件上传权限。实际 scope 名称以飞书开放平台当前版本控制台为准，先做只读/测试回调，再发布生产版本。

## 原始截图在飞书中的保存

当前 55 字段岗位表没有 JD 证据附件字段；按当前决定，原图只保留在本地收件证据目录和状态中，不新增岗位字段、收件表或可见后台字段。

## 安全边界

收件器只记录用户已投递事实、更新飞书状态和保留证据；它不自动申请岗位、登录招聘网站、发送招聘消息、绕过 CAPTCHA，也不把缺证据岗位提升为 READY/MUST_APPLY。常规自动发现的同步仍不会覆盖人工维护的 `Stage`、`Applied At`、跟进和面试字段。
