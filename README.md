# Track Job — Evidence-Driven Job Search OS

[English](#english) · [中文](#中文)

## English

Track Job is a personal job-search operating system built around one idea: every recommendation should be explainable, and every application should be recoverable.

### The story

Job searching usually fragments into browser tabs, spreadsheets, resume variants, and half-remembered application states. I designed this project as a small data platform rather than an “auto-apply bot”. It separates recall, precision, and human control:

- **Recall:** official ATS adapters, public-web discovery, and a human inbox surface opportunities that a fixed company allow-list would miss.
- **Precision:** a canonical job schema, source verification, freshness windows, deduplication, eligibility gates, and evidence-backed scoring turn noisy postings into reviewable candidates.
- **Human control:** collection is dry-run by default; risky actions and Feishu writes require explicit flags and preflight checks.

### What I designed and implemented

- A normalized job schema with provenance, observation time, verification state, source health, and application evidence.
- A two-lane discovery architecture for registered companies and newly discovered companies, with official ATS verification kept separate from supplemental sources.
- Deterministic scoring for opportunity, lifestyle fit, evidence quality, timing, and capacity; uncertain records are routed to `WATCH` / `HUMAN_REVIEW_REQUIRED` instead of being presented as facts.
- Cross-source deduplication, 14-day freshness checks, resumable runs, failure states, and Windows-friendly recovery paths.
- A Golden Base resume workflow that keeps evidence-backed content stable while allowing bounded, auditable tailoring for a role family.
- Feishu Bitable preview/upsert flows, interview-debrief capture, question-bank updates, analytics, and a next-day execution plan.

### End-to-end flow

```text
official / public-web / human discovery
  -> candidate inbox -> canonical verification -> normalize + deduplicate
  -> eligibility + evidence scoring -> WATCH / HOLD / READY / MUST_APPLY
  -> resume preview -> Feishu dry-run or explicit apply -> debrief + analytics
```

The design decision I am most proud of is the safety boundary: automation organizes information and exposes trade-offs, while the final application decision remains explicit and traceable.

### Quick start

Requires Python 3.10+.

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

The offline command exercises the decision pipeline without network access or Feishu writes. `--apply-feishu` is an explicit opt-in after preview and schema checks.

### Why this is useful in an interview

This repository demonstrates product thinking, data modelling, source reliability, failure-aware automation, document generation, and a practical understanding of where human review belongs in an AI-assisted workflow. The most important output is not a list of jobs; it is a defensible decision trail.

## 中文

Track Job 是一个面向真实求职流程的 **证据驱动型 Job Search OS**。它不把求职变成无人监管的“自动海投”，而是把岗位发现、核验、筛选、简历定制和投递复盘组织成一条可解释、可恢复、可审计的工程流水线。

### 我为什么做它

真实求职同时面对三个冲突：只盯着已知公司会漏掉新机会；只做开放网页搜索会混入过期、重复或无法核验的岗位；岗位、简历版本和投递状态分散在多个工具里，很难回答“为什么投、用了哪版简历、现在到哪一步”。

我的解决方案是把 **召回率、精确率和人工控制** 分开设计：

- 用官方 ATS 适配器、公开网页发现和人工收件箱提高召回；
- 用统一 schema、来源证据、更新时间、去重、Eligibility Gate 和证据评分提高精确度；
- 默认 dry-run，任何高风险动作和飞书写入都必须显式授权并通过预检。

### 我的核心工作

- 设计带来源 URL、观测时间、验证状态、来源健康度和投递证据的 canonical job schema；
- 建立“已登记公司 + 新发现公司”的双轨发现架构，并把官方 ATS 核验与补充来源分层；
- 实现机会、生活方式匹配、证据质量、时效和容量的确定性评分；不确定记录进入 `WATCH / HUMAN_REVIEW_REQUIRED`，不会被伪装成确定结论；
- 处理跨来源去重、14 天 freshness、可恢复运行、失败降级和 Windows 定时任务恢复；
- 设计 Golden Base 简历基线，在证据约束下为不同 role family 做有限、可审计的定制；
- 串起飞书预览/写入、面试复盘、问题库、分析报表和次日执行计划。

### 最小演示

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

离线命令可以在不联网、不写入飞书的情况下演示核心决策链路；只有在预览和 schema 检查通过后，才显式使用 `--apply-feishu`。

### 项目边界

系统不会绕过验证码、登录或招聘平台安全机制，也不会把不完整 JD 强行标成“可投递”。它的价值不是替人做最终决定，而是把每个决定背后的来源、证据、风险和下一步行动讲清楚。

更多实现映射见 [`docs/architecture.md`](docs/architecture.md)、[`docs/discovery-runbook.md`](docs/discovery-runbook.md) 和 [`examples/README.md`](examples/README.md)。
