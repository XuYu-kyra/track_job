# Track Job — Evidence-Driven Job Search OS

[English](#english) · [中文](#中文)

**Tech stack:** Python 3.10+ · Requests · Feishu/Lark OpenAPI · JSON/YAML configuration · PyMuPDF · LaTeX resume generation · `unittest` · Windows Task Scheduler support

## English

Track Job is a personal job-search operating system built around two rules: every recommendation should be explainable, and every application state should be recoverable. It is a data and decision pipeline—not an auto-apply bot.

### Problem model

Job-search data usually fragments across browser tabs, spreadsheets, resume variants, and application portals. The system separates three concerns that are often mixed together:

- **Recall:** official ATS adapters, public-web discovery, and a human inbox surface both known and previously unseen companies.
- **Precision:** canonical records, source verification, freshness, deduplication, eligibility rules, and evidence-based scoring turn noisy observations into reviewable candidates.
- **Control:** local collection is dry-run by default; Feishu writes and other consequential actions require explicit flags and preflight checks.

### Runtime architecture

```text
official ATS / public web / human discovery
  -> candidate inbox -> canonical verification -> normalize + deduplicate
  -> eligibility + evidence scoring -> WATCH / HOLD / READY / MUST_APPLY
  -> bounded resume tailoring -> preview -> explicit Feishu write
  -> application state -> interview debrief -> analytics + next-day plan
```

### Engineering highlights

- Designed a normalized job schema carrying source URLs, provenance, observation time, verification state, source health, lifecycle state, and application evidence.
- Kept official verification separate from supplemental discovery, including a two-lane model for registered and newly discovered companies.
- Implemented deterministic scoring for opportunity, lifestyle fit, evidence quality, timing, and capacity. Missing or conflicting evidence is routed to `WATCH` or `HUMAN_REVIEW_REQUIRED` rather than being silently upgraded.
- Added canonical-URL/job-ID identity rules, cross-source deduplication, 14-day freshness checks, resumable runs, explicit failure states, and Windows-friendly recovery.
- Built a Golden Base resume workflow that preserves evidence-backed content while allowing bounded, auditable tailoring by role family.
- Added Feishu Bitable preview/upsert flows, interview-debrief capture, question-bank updates, analytics, and execution-plan generation.
- Added compile checks, configuration validation, unit tests, and a privacy gate through `make check`.

### Safety boundary

The most important design choice is deliberate restraint. The pipeline can collect, compare, score, generate a preview, and explain its reasoning; it does not bypass logins or CAPTCHAs, and it does not turn incomplete job descriptions into facts. Final application decisions remain explicit and traceable.

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

This path exercises the decision pipeline without network access or Feishu writes. `--apply-feishu` remains an explicit opt-in after preview, schema validation, and source-coverage checks.

### Documentation

- [`docs/architecture.md`](docs/architecture.md): data contracts, discovery lanes, decision boundaries, resume selection, and privacy model.
- [`docs/discovery-runbook.md`](docs/discovery-runbook.md): operational discovery workflow.
- [`examples/README.md`](examples/README.md): example inputs and outputs.

## 中文

Track Job 是我为真实求职流程设计的 **证据驱动型 Job Search OS**。它不是自动海投工具，而是一条可解释、可恢复、可审计的数据与决策流水线：每个岗位为什么进入队列、依据来自哪里、用了哪版简历、当前走到哪一步，都能回溯。

### 问题拆分

求职信息通常散落在网页、表格、简历版本和投递平台中。这个项目把三个容易混在一起的目标分开处理：

- **召回率：** 通过官方 ATS、公开网页和人工收件箱，同时覆盖已知公司与新发现公司；
- **精确度：** 通过统一 schema、来源核验、时效、去重、Eligibility Gate 和证据评分，把噪声转成可检查的候选项；
- **人工控制：** 默认 dry-run，飞书写入和其他有后果的动作必须显式开启并通过预检。

### 工程实现

- 设计包含来源、观测时间、核验状态、来源健康度、生命周期和投递证据的统一岗位数据模型；
- 将补充来源发现与官方 ATS 核验分层，并支持“已登记公司 + 新发现公司”双轨流程；
- 实现机会价值、生活方式、证据质量、时效和容量的确定性评分；缺失或冲突信息进入 `WATCH / HUMAN_REVIEW_REQUIRED`，不会被包装成确定事实；
- 处理 canonical URL / job ID 身份规则、跨来源去重、14 天 freshness、可恢复运行和显式失败状态；
- 建立 Golden Base 简历基线，按 role family 做有限、可审计的内容调整，同时保留证据约束；
- 串起飞书预览/写入、面试复盘、问题库、分析报表和次日计划；
- 通过 `make check` 统一运行语法检查、配置验证、单元测试和隐私检查。

### 自动化边界

系统可以收集、比较、评分、生成预览并解释原因，但不会绕过登录或验证码，也不会把不完整 JD 强行判定为可投递。最终投递决定始终由人确认，并留下可追踪记录。

### 本地验证

上面的离线命令可在不联网、不写入飞书的情况下跑通核心决策链路。只有完成预览、schema 校验和来源覆盖检查后，才会显式使用 `--apply-feishu`。

架构细节见 [`docs/architecture.md`](docs/architecture.md)，发现流程见 [`docs/discovery-runbook.md`](docs/discovery-runbook.md)，示例见 [`examples/README.md`](examples/README.md)。
