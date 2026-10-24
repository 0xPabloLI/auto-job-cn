# Triage Labels

The triage skill uses five canonical roles. Label strings match role names.

## Category Labels

| Label         | Meaning                    |
| ------------- | -------------------------- |
| `bug`         | Something is broken        |
| `enhancement` | New feature or improvement |

## State Labels

| Label             | Meaning                                  |
| ----------------- | ---------------------------------------- |
| `needs-triage`    | Maintainer needs to evaluate             |
| `needs-info`      | Waiting on reporter for more information |
| `ready-for-agent` | Fully specified, ready for an AFK agent  |
| `ready-for-human` | Needs human implementation               |
| `wontfix`         | Will not be actioned                     |

## Priority Labels

Triage 时定档，复 triage 时可改。

| Label | Meaning                                                       |
| ----- | ------------------------------------------------------------- |
| `P0`  | 投递主链路阻断（发不出去 / 重复投递 / 风控误判放行）           |
| `P1`  | 主链路可用，但数据或质量受损                                   |
| `P2`  | 一般改进                                                       |
| `P3`  | 锦上添花，可延后                                               |

## Area Labels

| Label               | Meaning                                            |
| ------------------- | -------------------------------------------------- |
| `area:pipeline`     | 投递/追踪/知识库链路（live_apply、followup、kb）   |
| `area:platforms`    | 平台适配层（`scripts/platforms/`）                 |
| `area:agent-harness`| hooks、`docs/agents/`、session 基础设施            |
| `area:publish-chain`| 脱敏提升链 `scripts/publish/`、公开侧守卫、公开面文案改写（不是上游 `agent-harness` 那个仓） |
| `area:webui`        | `webui.py` 管理面板                                |
| `area:docs`         | 文档与调研材料                                     |

## Triage Flow

```
New issue → needs-triage (needs evaluation)
          → needs-info (waiting on clarification)
          → ready-for-agent (agent-pickable)
          → wontfix (won't fix)

needs-triage + clarified → ready-for-agent / ready-for-human
needs-info + reporter replies → needs-triage
```

## Conventions

- Every triaged issue carries exactly one category label, one priority label (P0-P3), one area label, and one state label.
- `ready-for-agent` requires: (1) fully specified with acceptance criteria, (2) correct priority, (3) actionable by an agent without further human input.
- `wontfix` issues should be closed with an explanation comment.
- Closing a completed issue: keep its category label, remove all state labels, priority labels (`P0`–`P3`) and area labels. Priority/area labels guide pick-up order for open backlog work only; on closed issues they are filter noise. `wontfix` is for rejected items only.
- Pick work only from `gh issue list --state open` — a closed issue is never a pickup candidate regardless of its labels.
