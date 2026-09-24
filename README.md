# Track Job — Evidence-Driven Job Search OS

这是一个面向 2027 校招的 **evidence-driven job search operating system**：把分散的招聘来源、岗位核验、岗位决策、角色化简历和投递反馈组织成一条可复现、可审计的工程流水线。

它不是“自动海投机器人”。系统把自动化用在信息整理和风险控制上，把最终投递决定留给人：默认只发现、归一化、评估、排队和生成预览；不会自动登录高风险招聘平台、绕过验证码或提交申请。日常流水线只有显式传入 `--apply-feishu` 才会写入飞书。

## 为什么做这个项目

真实求职流程有三个相互冲突的问题：

1. 只盯着已知公司，精确但容易漏掉新公司和临时开放的岗位；
2. 只做开放网页搜索，召回高但会混入过期、重复、地点不符或无法核验的岗位；
3. 岗位、简历和投递状态分散在不同工具里，最后很难解释“为什么投、用的哪版简历、现在进行到哪一步”。

这个项目的核心设计是把 **recall、precision 和 human control** 分开处理：

- 已知公司监控负责高精度和稳定性；
- 公司无关的公开市场搜索负责召回新机会；
- 规范化、来源证据和 Ready Gate 负责拦截不可靠结果；
- Golden Base 简历和投递收件箱负责让每次行动可追踪、可回放。

## 这个项目展示的工程能力

- **双轴发现**：公司 registry 是优先级和监控元数据，不是 allowlist；未注册公司仍可进入岗位池。
- **可靠性优先**：来源、开放状态、地点、毕业年份或 JD 不确定时，只进入 `WATCH/HUMAN_REVIEW_REQUIRED`，不会伪装成可投递岗位。
- **幂等与可恢复**：重复岗位、重复飞书消息、失败的抓取和中断的运行都有稳定键、状态和恢复路径。
- **安全边界**：采集和生成默认 dry-run；飞书写入、简历生成和高风险动作都需要显式门禁。
- **可审计输出**：每个岗位都能追溯到来源 URL、观察时间、验证状态、评分依据、简历基线和投递证据。
- **跨工具集成**：官方招聘站、LinkedIn、公开网页、飞书、GitHub 项目证据和简历生成被统一到同一套数据契约中。

如果把它作为求职作品讲解，可以用一句话概括：

> 我没有把求职自动化成无脑海投，而是把它设计成一个有来源证据、有覆盖率指标、有失败降级和人工决策边界的可靠系统。

## 适合面试的讲法

可以用下面四步介绍这个项目，而不是逐个罗列脚本：

1. **问题**：招聘信息分散、岗位会过期、同一岗位会在多个来源重复出现，投递状态和简历版本也容易失控。
2. **设计**：用“双轴发现”解决召回和精度的冲突，用统一岗位 schema、来源证据和 Ready Gate 控制质量。
3. **难点**：处理不完整 JD、来源限流、动态公司、重复事件、Windows 运行恢复、简历单页约束和飞书字段冻结。
4. **结果**：每个决策都能回溯到来源和证据；失败会降级为人工复核，而不是生成看似正确的错误结果。

这条叙事同时体现了产品意识、数据建模、可靠性工程、自动化集成和对风险边界的理解。

## 已实现的闭环

```text
deterministic monitoring / GPT-Codex public-web research / human discovery
  -> Candidate Job Inbox
  -> official/ATS canonical verification
  -> Source Adapters
  -> Canonical Job Schema
  -> cross-source dedup + 14-day freshness
  -> Eligibility Gate
  -> Opportunity + Lifestyle + Evidence
  -> Timing + Capacity + Ready Gate
  -> WATCH / HOLD / READY / MUST_APPLY / REJECT
  -> Feishu Bitable (preview by default)
  -> interview debrief -> Question Bank
  -> analytics + next-day Apple execution plan
```

关键规则已经固化：

- 主投 `TestDev / AI Application / Robot Software / General SWE / Backend`，FinTech、Data、Platform/SRE、Engineering Tools 等扩池。
- 只有明确护照集中保管或因私出境限制才硬拒绝；模糊 security/confidential/background-check 信息进入人工复核。
- `PRE_APPLICATION_HOLD` 阶段普通岗位只进入 `HOLD`；高价值、临期、高后悔值岗位可进入 `MUST_APPLY`。
- Capacity 只节流普通机会，不阻挡 `MUST_APPLY`。
- 岗位超过 14 天未验证标记为 `stale`，不继续出现在优先队列。
- 简历只为 `READY` 和 `MUST_APPLY` 生成，避免论文期批量制造过期材料。
- 飞书更新不会默认覆盖 `stage / next_action / interview debrief` 等人工维护字段。

计划到代码的完整映射见 [实施映射](docs/plan-implementation.md)，架构和字段见 [架构说明](docs/architecture.md)。

## 快速开始

要求 Python 3.10+。

```bash
python3 -m pip install -r requirements.txt
cp config/targets.example.yaml config/targets.yaml
cp config/feishu.example.yaml config/feishu.yaml
cp config/profile.example.yaml config/profile.yaml
cp config/execution.example.yaml config/execution.yaml
cp config/company_registry.example.yaml config/company_registry.yaml
```

这些本地配置已被 `.gitignore` 排除。请编辑：

- `config/targets.yaml`：阶段、地点、岗位族、WIP、Ready Gate、GitHub profile 与可选本地 Git 仓库路径。
- `config/feishu.yaml`：飞书凭据、Bitable token/table ID 和字段名。
- `config/profile.yaml`：本地 CN/INTL 双申请资料；`identity` 保留默认资料兼容层。
- `config/execution.yaml`：Reminders 列表名、Calendar 名和学习模块。
- `config/company_registry.yaml`：重点公司与来源 registry。

先跑离线/只读模式：

```bash
python3 scripts/run_daily_pipeline.py --skip-fetch --skip-documents
```

这会生成决策结果、Analytics、Apple execution plan 和飞书 upsert 预览，但不会写飞书。

允许在线采集、仍保持飞书 dry-run：

```bash
python3 scripts/run_daily_pipeline.py
```

只有确认预览和飞书表结构正确后，才显式允许写入：

```bash
python3 scripts/run_daily_pipeline.py --apply-feishu
```

## 最短可复现演示

不接入真实账号也可以展示核心能力：

```bash
make check
python3 scripts/build_public_web_query_plan.py
python3 scripts/build_discovery_coverage.py
python3 scripts/run_daily_pipeline.py --skip-fetch --skip-documents
```

建议演示四个结果：岗位规范化和去重、覆盖率报告、READY/HOLD/REJECT 决策、以及飞书 dry-run 预览。这样能说明系统不仅“能抓到数据”，还知道什么时候应该拒绝自动化。

## 飞书初始化

先打印所需字段，不联网：

```bash
python3 scripts/setup_feishu.py
```

只读比较现有表结构：

```bash
python3 scripts/setup_feishu.py --check
```

创建缺失字段（不会修改或删除已有字段）：

```bash
python3 scripts/setup_feishu.py --apply
```

对于现有单选/多选字段，`--check` 还会报告缺少的 options；`--apply` 只用于首次初始化或管理员维护。本项目不会自动调用这个命令。初始化完成后 55 字段 schema 视为冻结；正常同步只做 schema preflight 和记录 upsert，不创建、改名、改类型或追加 options。

权限、字段类型和人工字段保护说明见 [飞书落地指南](docs/feishu-setup.md)。

## 岗位输入

来源采用确定性监控、GPT/Codex 公网语义发现和人工收件箱三路汇合；官方/ATS 用作规范验证，LinkedIn/Indeed 仅作 supplemental。详细步骤见 [Discovery runbook](docs/discovery-runbook.md)，Cursor/Codex 指令见 [MANUAL_AGENT 模板](docs/gpt-discovery-agent.md)。人工输入格式为：

```text
URL | Company | Position | Location | Description | Deadline | Source | Dream role | Notes | Application profile
```

先把 `data/job_cache/manual_job_inputs.example.txt` 复制为已被 Git 忽略的 `data/job_cache/manual_job_inputs.txt`，再把真实内容加入运行时文件并执行：

```bash
python3 scripts/import_manual_jobs.py
python3 scripts/merge_job_sources.py
python3 scripts/score_jobs.py
```

可复现样例位于 [examples](examples/README.md)。

飞书投递收件箱当前按“一条消息对应一个岗位”处理：同一岗位的多个证据可以一起发送，不同岗位请分开发送。这样每条消息都能独立去重、匹配和写入已投递表，避免把多个 JD 合并成一条错误记录。

## Source Discovery V3：双轴发现

官方源不再按公司截断为 4 条。`scripts/official_adapters.py` 为 Workday、
Beisen、Hotjob、Feishu Recruiting、JSON-LD、sitemap 和稳定服务端 HTML
提供统一的 `probe/list/detail/normalize/health` 接口；每家公司完整翻页，默认
最多收集 200 条。达到上限、翻页中断或限流时结果是 `PARTIAL`，不会伪装成
`SUCCESS`。公开 JavaScript 页面只允许使用可选的只读 Playwright 网络观察器，
不得登录、绕过 CAPTCHA 或提交申请。

先审计 registry，再执行只读发现：

```bash
python3 scripts/audit_company_sources.py
python3 scripts/fetch_official.py --max-links-per-company 200
python3 scripts/fetch_linkedin.py --batch-index -1
python3 scripts/build_public_web_query_plan.py
```

公司审计输出 `company_source_audit.json/.md`，只给出推荐分类，不会自动把公司
改成 `AUTO`。公司 registry 只是优先级与监控元数据，不是 allowlist。发现采用
双轴结构：39 家 P0 每日监控，P1 按确定性的三日滚动计划全覆盖；同时按配置中的
地点、毕业年份、核心/扩展岗位别名、公开来源域名、ATS 域名和校招批次词生成
不带公司名的开放市场查询。牛客、NCSS、国聘、学校就业站、通用公网和官方域名
发现分别记账，MANUAL_AGENT 必须逐条执行计划并回传回执。

未登记公司的岗位仍会正常归一化、去重、评分并获得简历资格。系统将它们保存在
`data/job_cache/discovered_company_candidates.json`；找到已验证官方/ATS URL、
连续多次新鲜观察、出现多个目标岗位，或产生 `READY/MUST_APPLY` 机会时，该公司
进入动态持续监控，后续查询计划增加 `discovered_company_monitoring` shard，但不
自动修改 registry。LinkedIn 批次由上海日期确定，首次 429 后立即停止并记录
`PARTIAL_RATE_LIMITED`，缓存回放不会冒充本次实时观察。

归一化岗位带有 `DISCOVERED_CANDIDATE / VERIFIED_OPEN_JOB / CLOSED /
UNCERTAIN` 验证状态。状态、时效、毕业年份、深圳地点或完整 JD 任何一项不确定，
都只能进入 `WATCH/HUMAN_REVIEW_REQUIRED`。最终运行
`python3 scripts/build_discovery_coverage.py` 生成覆盖报告。报告同时给出 P0
日覆盖、P1 三日滚动覆盖、公司无关查询、核心/扩展岗位、
六个公开来源 lane、新公司/官方 URL/晋级与逾期 shard 指标。默认只有
`SOURCE_COVERAGE_HEALTHY` 才允许显式飞书 apply；在所有查询都有终态回执且
没有 unsafe `READY/MUST_APPLY` 时，经过明确授权也可使用
`--allow-degraded-review-sync` 同步人工复核池，但 WATCH/REJECT 永不上传简历
附件，READY/MUST_APPLY 仍逐条通过开放性与 page-fit 门禁。dry-run 和本地报告不受影响。
详细操作及回滚方式见 [Discovery runbook](docs/discovery-runbook.md)。

## 候选人技能与项目证据

候选人事实的唯一人工维护来源是 `cv/materials/`；`data/job_cache/candidate_inventory.json` 与 `data/job_cache/semantic_candidate_evidence.json` 都是可随时重建的运行时产物。

远程 GitHub profile 使用独立同步步骤发现并筛选公开仓库，再浅克隆到 `data/candidate_repos/`；`repo_paths` 始终只保存本地路径：

```bash
python3 scripts/sync_candidate_repos.py
```

`build_candidate_inventory.py` 会从 `cv/materials/evidence.yaml` 读取带来源类别的技能证据，并仅扫描 `projects.yaml` 明确列出的同步仓库（显式本地 `repo_paths` 仍保留），输出技能等级及 repo/file 证据：

```bash
python3 scripts/build_candidate_inventory.py
python3 scripts/build_candidate_evidence.py
```

第二条命令把材料库规范化为 DeepSeek 只读的匿名 JSON 输入。`VERIFIED`、`USER_ATTESTED` 和 `DERIVED_RESUME_SAFE` 可进入该输入；`UNCERTAIN` 会被排除。真实身份、仓库路径和所有生成文件仍只保留在本地。

## Golden Base 简历架构

简历生成默认不再从一个通用模板重新拼装，而是先按 canonical
`role_family` 选择已经批准的 Golden Base：

- `ai`：AI application；
- `robot`：robot software；
- `se`：general software、backend 以及相关扩展岗位；
- `test`：test development，以及以验证/测试为主的 reliability 岗位。

每个角色基线包含中文无照片、中文照片和英文无照片三种版本，共 12
个稳定 base ID，注册在 `config/resume_bases.yaml`。默认始终选择无照片；
中文可通过 `--resume-variant photo` 显式选择照片版。英文没有获批照片版，
请求 EN + photo 默认报错，只有同时传入
`--allow-english-photo-fallback` 才会明确回退到英文无照片版本。

原始 `cv/*_xuyu.zip` 是工作源。日常修改同一套工作基线后，不要手工改哈希、
解压目录或基准值；运行显式同步命令即可更新注册哈希、解压出的 `main.tex`、
照片资产、规范化 marker 模板、编译 PDF、基准缓存和注册表中的基准值：

```bash
python3 scripts/sync_resume_base_sources.py --role robot
# 或：make sync-robot-bases
```

普通安全导入仍会验证 ZIP SHA-256、拒绝 zip-slip 路径，并拒绝静默覆盖同一
版本 ID 下已经变化的文件：

```bash
python3 scripts/import_resume_bases.py
python3 scripts/benchmark_resume_bases.py
```

JD 定制只能在 base 上进行有证据约束的排序、少量 bullet 替换和
compact/standard/expanded 变体切换。默认至少保留 70% 实质槽位，最多修改
30%；越界时状态为 `HUMAN_REVIEW_REQUIRED`，不会退回“从空白计划重建”。
每个产物记录 base ID、版本、源 ZIP 哈希、照片变体、定制比例、事实验证与
page-fit 状态。只有全部硬门禁通过的单页 PDF 才能进入飞书附件上传。

上面的同步适合持续维护当前工作基线。需要可审计发布、保留历史或随时回滚时，
仍应使用新的稳定版本 ID 和目录，而不是刷新旧版本：先放置新的不可变 ZIP，
登记资产与 section order，运行安全导入和 base 基准测试，再补充证据映射与
回归测试。已经按某个 JD 生成的 `cv/generated/` 岗位简历属于历史产物，不会
被基线同步静默改写；下一次为该岗位重新生成时会自动使用新基线和新哈希。

## Initial backfill 与 daily incremental

日常任务保持 72 小时、小批量和原有 `max_jobs_per_run`：

```bash
python3 scripts/run_daily_pipeline.py --mode daily
```

首次启动使用独立的 60 天、分批 backfill；它只运行到本地 normalize/dedup/eligibility/score/queue，不生成 CV，也不会进入飞书步骤：

```bash
python3 scripts/run_daily_pipeline.py --mode backfill --batch-index 0
```

检查 `data/job_cache/backfill_scored_jobs.json` 后再决定是否人工导入。不同 `batch-index` 会轮换有界 query matrix。

## 面试反馈与 Question Bank

面试后按 [debrief 样例](examples/interview_debrief.sample.json) 记录问题、结果、团队和轮次：

```bash
python3 scripts/update_question_bank.py \
  --input examples/interview_debrief.sample.json
```

系统按规范化问题去重，累计出现频率，并更新 `unknown / learning / weak / strong`。弱项会进入之后的 execution plan。

## Apple Reminders / Calendar

程序输出 `data/job_cache/execution_plan.json`，其中包含：

- `reminders`：目标列表、日期、标签、优先级和预计时长。
- `calendar_blocks`：`Study & Job` 日历的开始/结束时间。

它是计划中两个 Apple Shortcuts 的稳定数据契约；程序本身不直接控制 iCloud。Daily Check-in 可按 [样例](examples/daily_checkin.sample.json) 回传完成率：

```bash
python3 scripts/build_execution_plan.py \
  --check-in examples/daily_checkin.sample.json
```

完成率 `<50%` 时自动降载约 30%；连续三天低完成会标记整周重估。详细日常操作见 [运行手册](docs/operations.md)。

## 阶段切换

论文结束后，在 `config/targets.yaml` 修改：

```yaml
operating_mode:
  phase: ACTIVE_APPLICATION
  dissertation_active: false
```

然后逐项更新 `ready_to_apply`。只有 Ready Gate 达标且 72 小时负荷允许，普通高质量岗位才会从 `HOLD` 释放为 `READY`；`MUST_APPLY` 不受此限制。

## 验证

```bash
make check
```

没有 `make` 时运行等价命令：

```bash
python3 -m py_compile scripts/*.py
python3 scripts/validate_config.py
python3 -m unittest discover -s tests -v
python3 scripts/privacy_check.py
```

## 隐私边界

仓库不会跟踪真实身份、飞书凭据、岗位缓存、Question Bank、Analytics、execution plan 或生成材料。发布前必须运行 `python3 scripts/privacy_check.py`。
