# Track Job — Evidence-Driven Job Search OS

[English](#english) · [中文](#中文)

Track Job is a personal job-search operating system that turns scattered vacancies, resume variants, application records, and interview notes into one explainable workflow. It is designed around a simple rule: **automation may organize evidence and prepare actions, but it should not invent facts or take consequential actions without confirmation.**

## English

### What it solves

A serious job search quickly becomes a data problem. The same vacancy may appear through several sources; job pages expire; eligibility details are ambiguous; resume variants drift; and application history becomes difficult to reconstruct.

I built Track Job to separate that problem into four stages:

1. **Discover** opportunities from official ATS pages, public sources, and a human-maintained inbox.
2. **Verify** identity, provenance, freshness, graduation eligibility, and location evidence before ranking.
3. **Decide** through deterministic gates and component scores instead of an opaque recommendation.
4. **Act and learn** through bounded resume tailoring, explicit Feishu writes, application-state tracking, and interview debriefs.

This is not an auto-apply bot. It is a recoverable decision pipeline that keeps the human at the final application boundary.

### System flow

```text
official ATS / public web / manual discovery
                │
                ▼
        candidate job inbox
                │
                ▼
 canonical verification + normalization
                │
                ▼
 identity / deduplication / freshness history
                │
                ▼
 eligibility gate + evidence-based scoring
                │
                ▼
 WATCH · HOLD · READY · MUST_APPLY
                │
                ▼
 resume preview + explicit Feishu write
                │
                ▼
 application state · debrief · analytics · next plan
```

### Engineering decisions

#### 1. Treat provenance as data, not a footnote

The canonical job record carries source URLs, observation origin, verification level, timestamps, source health, lifecycle state, application evidence, and interview state. A lead found on a secondary source can remain useful without being mistaken for a verified open position.

Identity follows a strict hierarchy: verified canonical URL, then source-scoped stable job ID, then a normalized company/title/location/cohort fallback. Observations from multiple sources are merged without collapsing unrelated requisition IDs.

#### 2. Separate discovery from verification

Discovery aims for recall; verification protects precision. Official adapters, public-web query plans, and manual imports all feed the same normalization and scoring path, but only a currently verified job with sufficient evidence can move to `READY` or `MUST_APPLY`.

Missing or conflicting graduation, location, JD, or source evidence is preserved as uncertainty and routed to `WATCH` or human review. The system never upgrades an incomplete description into a fact.

#### 3. Make every recommendation inspectable

Eligibility hard filters run before opportunity ranking. The ranking layer keeps opportunity value, lifestyle fit, evidence confidence, timing, regret, and current process capacity as separate components. Queue decisions are therefore reproducible and can explain both *why a job is attractive* and *why it may not be actionable today*.

#### 4. Build for interruption and recovery

The pipeline retains observation history, distinguishes live fetches from cache replay, records explicit failure states, and supports deterministic `--as-of` runs. Windows scheduling helpers and resumable intermediate artifacts allow a failed daily run to be diagnosed and continued instead of silently losing state.

#### 5. Keep document generation evidence-bounded

Resume generation starts from a versioned Golden Base selected by role family. Tailoring may reorder or replace bounded slots, but it remains tied to candidate evidence IDs and reports what changed. Preview, page-fit, fact, and evidence gates run before a document becomes eligible for use.

### Tech stack

| Area | Technologies and responsibilities |
|---|---|
| Core pipeline | Python 3.10+, typed JSON-style records, modular command-line stages |
| Collection | `requests`, official ATS adapters, structured/public-web discovery, manual JSON inbox |
| Configuration | YAML/JSON examples, schema and cross-file validation |
| Decision layer | deterministic eligibility rules, component scoring, queue-state machine |
| Documents | LaTeX resume generation, PyMuPDF-based output inspection, evidence manifests |
| Workspace integration | Feishu/Lark OpenAPI, Bitable preview and idempotent upsert flows |
| Reliability | `unittest`, fixtures, compile checks, privacy checks, source-health and coverage gates |
| Operations | Make targets, Windows Task Scheduler support, logs and resumable caches |

### Safety and control model

- Collection and local processing are safe to run without writing to Feishu.
- `--apply-feishu` is an explicit opt-in after preview and validation.
- Login or CAPTCHA bypass is outside the system boundary.
- Unknown lifestyle or eligibility information remains unknown rather than becoming a score-friendly assumption.
- Runtime configuration, generated resumes, caches, logs, and personal data remain outside version control; the privacy check covers the working tree, index, and reachable history.

### Run the offline verification path

```bash
python3 -m pip install -r requirements.txt
cp config/targets.example.yaml config/targets.yaml
cp config/feishu.example.yaml config/feishu.yaml
cp config/profile.example.yaml config/profile.yaml
cp config/execution.example.yaml config/execution.yaml
cp config/company_registry.example.yaml config/company_registry.yaml

make check
python3 scripts/run_daily_pipeline.py --skip-fetch --skip-documents
```

This path exercises the validation and decision stages without external collection or Feishu writes. See [`docs/architecture.md`](docs/architecture.md) for the data contracts and decision boundaries, [`docs/discovery-runbook.md`](docs/discovery-runbook.md) for the operating workflow, and [`examples/README.md`](examples/README.md) for sample inputs and outputs.

---

## 中文

Track Job 是我围绕真实求职流程设计并实现的一套 **证据驱动型求职管理系统**。它把分散在招聘网页、简历版本、飞书表格和面试记录里的信息，整理成一条可解释、可恢复、可检查的工作流。

项目遵循一个核心原则：**自动化可以负责收集证据、整理信息和生成待确认操作，但不能替人编造事实，也不能越过人工确认直接执行关键动作。** 因此它不是自动海投工具，而是一套帮助我稳定做出投递决策的个人系统。

### 为什么做这个项目

当求职目标、信息来源和简历版本逐渐增多时，真正困难的并不是“把岗位存进表格”，而是持续回答这些问题：

- 同一岗位在多个来源出现时，怎样判断它们是否属于同一个 requisition？
- 一个岗位是刚刚发现、仍在招聘，还是只剩下过期缓存？
- 校招年份、工作地点和职位描述不完整时，系统应该推荐还是暂缓？
- 为什么某个岗位被标记为优先投递，判断依据能否被复查？
- 生成的简历是否仍然基于真实经历，使用了哪一份基线，又修改了哪些内容？
- 一次运行中断后，能否从已有状态继续，而不是重新抓取并覆盖记录？

Track Job 将这些问题拆成“发现—核验—决策—执行与复盘”四个阶段，并为每一步保存来源、状态和判断依据。

### 整体流程

```text
官方 ATS / 公开网页 / 人工发现
              │
              ▼
          候选岗位收件箱
              │
              ▼
      官方核验 + 统一数据结构
              │
              ▼
   身份判定 / 跨来源去重 / 时效历史
              │
              ▼
       资格门槛 + 证据化评分
              │
              ▼
 WATCH · HOLD · READY · MUST_APPLY
              │
              ▼
      简历预览 + 显式写入飞书
              │
              ▼
  投递状态 · 面试复盘 · 分析 · 次日计划
```

### 核心工程设计

#### 1. 把来源与可信度纳入数据模型

统一岗位记录不仅保存公司、职位和链接，也保存来源 URL、观测方式、核验等级、首次发现与最近核验时间、来源健康度、岗位生命周期、投递证据和面试状态。这样，来自二手渠道的线索可以继续保留，但不会被误认为已经由官方确认的在招岗位。

岗位身份采用分层规则：优先使用已核验的 canonical URL，其次使用带来源命名空间的稳定 job ID，最后才使用公司别名、职位、地点和校招届别组成的归一化键。跨来源信息可以合并，同时避免因为编号碰巧相同而错误折叠两个岗位。

#### 2. 将“发现岗位”和“确认岗位”分开

发现阶段追求覆盖率，核验阶段负责控制准确性。官方 ATS adapter、公开网页检索计划和人工导入都进入同一条标准化流水线，但只有状态仍为开放、JD 足够完整、届别和地点证据满足要求的岗位，才有资格进入 `READY` 或 `MUST_APPLY`。

如果证据缺失或互相冲突，系统会保留不确定性，将岗位送入 `WATCH` 或人工复核，而不是为了提高命中率而把模糊信息解释成确定事实。

#### 3. 让推荐结论可以复算

资格硬门槛先于排序执行。进入评分阶段后，系统分别计算机会价值、生活方式匹配、证据置信度、时效、错过成本和当前流程容量，而不是用一个不可解释的总分覆盖所有判断。

因此，队列状态既能说明“这个岗位本身值不值得考虑”，也能说明“它为什么现在适合投、需要暂缓，或仍应继续观察”。所有中间结果都会写入结构化文件，方便检查规则和回放决策。

#### 4. 为中断、失败和重复运行设计

流水线保留历史观测，明确区分实时抓取、缓存回放、人工导入和官方核验；失败会以显式状态和日志保存。`--as-of` 支持确定性回放，Windows 调度辅助脚本和可恢复的中间产物则让日常任务在中断后能够定位问题并继续执行。

#### 5. 约束简历生成的可修改范围

简历生成从按岗位族选择的 Golden Base 开始。系统只允许在有限槽位内重排或替换内容，并要求自动使用的经历与能力能够对应到候选人证据 ID。每次生成都会记录基线、改动比例和使用的证据，同时经过预览、版面、事实与证据检查。

这一设计的重点不是“生成更多文案”，而是让不同岗位的简历调整保持一致、可追踪，并避免为了匹配 JD 添加未经证明的能力。

### 技术栈与实际用途

| 模块 | 技术与职责 |
|---|---|
| 核心流水线 | Python 3.10+、模块化命令行阶段、统一的结构化岗位记录 |
| 数据采集 | `requests`、官方 ATS adapter、结构化网页来源、人工 JSON 收件箱 |
| 配置管理 | YAML / JSON 示例配置、schema 与跨文件一致性校验 |
| 决策系统 | 确定性资格规则、分项评分、岗位队列状态机 |
| 文档生成 | LaTeX 简历、PyMuPDF 输出检查、证据 manifest |
| 工作台集成 | 飞书/Lark OpenAPI、Bitable 预览与幂等 upsert |
| 质量保障 | `unittest`、fixtures、语法检查、隐私检查、来源健康度与覆盖率门槛 |
| 日常运行 | Make 任务、Windows Task Scheduler 支持、日志与可恢复缓存 |

### 自动化边界

- 本地采集和决策流程可以在不写入飞书的情况下运行；
- 真正写入飞书必须显式传入 `--apply-feishu`，并先通过预览与配置检查；
- 系统不绕过登录或验证码；
- 无法确认的届别、地点和生活方式信息会继续保持未知，不会被转化为有利分数；
- 个人配置、生成简历、运行缓存和日志不进入版本控制，隐私检查同时覆盖工作区、Git index 和可达历史。

### 本地验证

```bash
python3 -m pip install -r requirements.txt
cp config/targets.example.yaml config/targets.yaml
cp config/feishu.example.yaml config/feishu.yaml
cp config/profile.example.yaml config/profile.yaml
cp config/execution.example.yaml config/execution.yaml
cp config/company_registry.example.yaml config/company_registry.yaml

make check
python3 scripts/run_daily_pipeline.py --skip-fetch --skip-documents
```

这条离线路径可以在不执行外部采集、不写入飞书的情况下验证配置、测试和核心决策链路。更详细的数据契约与状态边界见 [`docs/architecture.md`](docs/architecture.md)，日常发现流程见 [`docs/discovery-runbook.md`](docs/discovery-runbook.md)，示例输入输出见 [`examples/README.md`](examples/README.md)。

