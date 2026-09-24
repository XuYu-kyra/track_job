# Feishu Bitable Setup

## 1. Create and authorize the app

Create a Feishu enterprise self-built app, enable Bitable access, publish the app version, and add the app to the target Bitable document with edit permission. Attachment upload additionally needs the corresponding Drive/media permission.

Useful official references:

- [Create field API](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/create)
- [List fields API](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-field/list)
- [Create record API](https://open.feishu.cn/document/server-docs/docs/bitable-v1/app-table-record/create)

## 2. Configure locally

```bash
cp config/feishu.example.yaml config/feishu.yaml
```

Fill the local file or use environment variables:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_APP_TOKEN`
- `FEISHU_TABLE_ID`
- `FEISHU_VIEW_ID` (optional)

Never commit the live values.

## 3. Prepare fields safely

`setup_feishu.py` has three modes:

```bash
# Offline specification only
python3 scripts/setup_feishu.py

# Read-only live comparison
python3 scripts/setup_feishu.py --check

# Create missing fields and append missing select options
python3 scripts/setup_feishu.py --apply
```

`--apply` is an initialization/administrator command. It creates missing fields and appends missing options to existing single/multi-select fields. It never deletes fields, removes options, or changes an existing field type. Before any schema write, the complete live schema is compared; extra fields, rogue options, and type mismatches abort the operation for manual review. The daily and backfill pipelines never call schema `--apply` automatically. After initialization, the exact 55-field name/type/select-option contract is frozen.

## 4. Preview job upserts

```bash
python3 scripts/update_feishu.py --dry-run
```

Dry-run is also the default. It does not authenticate or call Feishu. A live write requires:

```bash
python3 scripts/update_feishu.py --apply
```

Before the first record or attachment write, production sync fetches the live field definitions and performs a blocking exact comparison against the frozen field-name set, types, and select-option sets. Missing/extra/renamed fields, wrong types, and missing/extra options abort the entire sync with zero record writes. Production sync never creates, renames, retypes, or appends options.

With `attachments.upload_generated_files: true`, generated PDFs are uploaded only in explicit `--apply` mode. A dry-run never uploads files.

## 5. Ownership of fields

Automation owns identity, freshness, decision scores, timing, and queue action.

Humans own application and interview progress, including:

- `stage`
- `next_action` / `next_deadline`
- `resume_version`
- `round`, `team`, `manager_signal`
- `questions`, `weak_topics`, `debrief`
- `offer`

Updates do not write these fields while `sync.update_human_managed_fields` is `false`. This prevents a daily sync from resetting progress maintained in Feishu.

## 6. Recommended views

Create views using the stable fields:

- Must Apply: `Action = MUST_APPLY`
- Ready Queue: `Action = READY`
- Dissertation Hold: `Action = HOLD`
- Needs Research: `Action = WATCH` or `Evidence Confidence = Low`
- Active Process: `Stage` in `APPLIED/OA/INTERVIEW`
- Stale Verification: `Freshness = stale`
- Reject Analytics: `Action = REJECT`, grouped by `Reject Reason`
- Submitted: `Action = APPLIED`
- Active Process: `Action = PROCESS`
- Offers: `Action = OFFER`
