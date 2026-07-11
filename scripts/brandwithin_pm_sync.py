#!/usr/bin/env python3
"""Cloud PM sync for the Brandwithin client dashboard."""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


SLACK_CHANNEL_ID = "C0B4RDY3TDF"
CLICKUP_LIST_ID = "901615501737"
CLICKUP_TASK_URL = "https://app.clickup.com/t/"
TIMEZONE = ZoneInfo("Asia/Manila")

ASSIGNEE_IDS = {
    "mj": 82518853,
    "mj atrero": 82518853,
    "troy": 100890201,
    "kenlie": 101110845,
    "kenlie carreon-yang": 101110845,
}

CLICKUP_PRIORITY = {
    "urgent": 1,
    "high": 2,
    "normal": 3,
    "low": 4,
}

ALLOWED_STATUSES = {"to do", "in progress", "for checking", "complete"}


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


SLACK_BOT_TOKEN = require_env("SLACK_BOT_TOKEN")
CLICKUP_API_TOKEN = require_env("CLICKUP_API_TOKEN")
OPENAI_API_KEY = require_env("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"


def http_json(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    timeout: int = 45,
) -> dict:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {raw}") from exc


def with_params(url: str, params: dict[str, str | int | float | bool]) -> str:
    return f"{url}?{urllib.parse.urlencode(params)}"


def slack_get(method: str, params: dict[str, str | int | float]) -> dict:
    url = with_params(f"https://slack.com/api/{method}", params)
    data = http_json("GET", url, headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"})
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error for {method}: {data.get('error', data)}")
    return data


def slack_window_start(now: dt.datetime) -> tuple[dt.datetime, str]:
    backfill_hours = os.environ.get("BACKFILL_HOURS", "").strip()
    if backfill_hours:
        try:
            hours = max(1, min(168, int(backfill_hours)))
            return now - dt.timedelta(hours=hours), f"{hours} hours"
        except ValueError:
            pass

    if now.weekday() == 0 and 5 <= now.hour <= 10:
        return now - dt.timedelta(hours=72), "72 hours"

    return now - dt.timedelta(minutes=75), "75 minutes"


def resolve_slack_names(messages: list[dict]) -> dict[str, str]:
    user_ids: set[str] = set()
    for message in messages:
        if message.get("user"):
            user_ids.add(message["user"])
        for match in re.findall(r"<@([A-Z0-9]+)>", message.get("text", "")):
            user_ids.add(match)

    names: dict[str, str] = {}
    for user_id in sorted(user_ids):
        try:
            info = slack_get("users.info", {"user": user_id})
            user = info.get("user", {})
            profile = user.get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
            names[user_id] = name
        except Exception:
            names[user_id] = user_id
    return names


def clean_slack_text(text: str, names: dict[str, str]) -> str:
    text = re.sub(r"<@([A-Z0-9]+)>", lambda m: f"@{names.get(m.group(1), m.group(1))}", text)
    text = re.sub(r"<([^|>]+)\|([^>]+)>", lambda m: m.group(2), text)
    text = re.sub(r"<([^>]+)>", lambda m: m.group(1), text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_slack_messages(now: dt.datetime) -> tuple[list[dict], str]:
    oldest_dt, label = slack_window_start(now)
    oldest = oldest_dt.timestamp()
    messages: list[dict] = []
    cursor = ""

    for _ in range(3):
        params: dict[str, str | int | float] = {
            "channel": SLACK_CHANNEL_ID,
            "oldest": oldest,
            "limit": 100,
            "inclusive": "true",
        }
        if cursor:
            params["cursor"] = cursor
        data = slack_get("conversations.history", params)
        messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break

    names = resolve_slack_names(messages)
    cleaned = []
    for message in messages:
        if message.get("subtype") in {"bot_message", "message_deleted"}:
            continue
        ts = float(message.get("ts", "0"))
        when = dt.datetime.fromtimestamp(ts, TIMEZONE)
        cleaned.append(
            {
                "ts": message.get("ts", ""),
                "date": when.strftime("%Y-%m-%d %H:%M %Z"),
                "user": names.get(message.get("user", ""), message.get("user", "unknown")),
                "text": clean_slack_text(message.get("text", ""), names),
            }
        )

    cleaned.sort(key=lambda item: item["ts"])
    return cleaned, label


def clickup_url(path: str, params: dict[str, str | int | bool] | None = None) -> str:
    url = f"https://api.clickup.com/api/v2/{path.lstrip('/')}"
    return with_params(url, params) if params else url


def clickup_request(method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
    return http_json(
        method,
        clickup_url(path, params),
        headers={"Authorization": CLICKUP_API_TOKEN},
        payload=payload,
    )


def fetch_clickup_tasks() -> list[dict]:
    tasks: list[dict] = []
    for page in range(25):
        data = clickup_request(
            "GET",
            f"list/{CLICKUP_LIST_ID}/task",
            params={"include_closed": "true", "subtasks": "true", "page": page},
        )
        page_tasks = data.get("tasks", [])
        tasks.extend(page_tasks)
        if data.get("last_page") or not page_tasks:
            break
    return tasks


def timestamp_ms_to_dt(value: str | int | None) -> dt.datetime | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return dt.datetime.fromtimestamp(int(value) / 1000, TIMEZONE)
    except Exception:
        return None


def task_status(task: dict) -> str:
    status = task.get("status") or {}
    if isinstance(status, dict):
        return str(status.get("status", "")).lower()
    return str(status).lower()


def task_priority(task: dict) -> str:
    priority = task.get("priority")
    if isinstance(priority, dict):
        return str(priority.get("priority", "normal")).lower()
    return "normal"


def assignee_names(task: dict) -> list[str]:
    names = []
    for assignee in task.get("assignees") or []:
        name = assignee.get("username") or assignee.get("email") or assignee.get("id")
        if name:
            names.append(str(name).split()[0])
    return names


def assignee_label(task: dict) -> str:
    names = assignee_names(task)
    if names:
        return " & ".join(names)

    title = task.get("name", "")
    owner_patterns = [
        r"\((James|Charmene|Jacinta|Kim|Client|Admin)\)",
        r" - (James|Charmene|Jacinta|Kim|Client|Admin)(?:\b| &)",
    ]
    for pattern in owner_patterns:
        match = re.search(pattern, title, flags=re.I)
        if match:
            return match.group(1).title()
    return "Unassigned"


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def looks_like_existing_task(title: str, tasks: list[dict]) -> bool:
    new_norm = normalize_text(title)
    if not new_norm:
        return True
    new_words = set(new_norm.split())
    for task in tasks:
        old_norm = normalize_text(task.get("name", ""))
        if old_norm == new_norm:
            return True
        if new_norm in old_norm or old_norm in new_norm:
            return True
        old_words = set(old_norm.split())
        if len(new_words) >= 4 and old_words:
            overlap = len(new_words & old_words) / max(len(new_words), len(old_words))
            if overlap >= 0.82:
                return True
    return False


def summarize_tasks_for_ai(tasks: list[dict]) -> list[dict]:
    summarized = []
    for task in tasks:
        summarized.append(
            {
                "id": task.get("id"),
                "name": task.get("name"),
                "status": task_status(task),
                "priority": task_priority(task),
                "assignees": assignee_names(task) or [assignee_label(task)],
                "due_date": (timestamp_ms_to_dt(task.get("due_date")) or ""),
                "parent": task.get("parent"),
            }
        )
    return summarized


def ask_ai_for_plan(slack_messages: list[dict], tasks: list[dict], now: dt.datetime) -> dict:
    system = (
        "You are a careful AI project manager automation. Return only valid JSON. "
        "Never invent Slack messages. Never delete anything. Be conservative with ClickUp writes."
    )
    user = {
        "now": now.strftime("%Y-%m-%d %H:%M %Z"),
        "project": "Mariangela Parodi - Website Build",
        "slack_messages": slack_messages,
        "clickup_tasks": summarize_tasks_for_ai(tasks),
        "rules": [
            "Create a ClickUp task only for actionable Slack requests with no matching task.",
            "Split grouped TO DO SUMMARY bullets into separate tasks.",
            "Comment on matched tasks for relevant status updates.",
            "If comments fail, the script will use a concise title suffix fallback.",
            "Status updates allowed: to do, in progress, for checking, complete.",
            "Only mark complete when an assignee reports done/fixed/completed.",
            "Only set due dates from explicit Slack dates or relative dates grounded in message time.",
            "Assignees allowed: MJ, Troy, Kenlie. James, Charmene, Jacinta, Kim, and Client stay unassigned.",
            "Digest lines must be under 25 words each.",
        ],
        "required_json_shape": {
            "clickup_actions": [
                {
                    "type": "create_task | comment_task | update_task",
                    "task_id": "existing task id when applicable",
                    "title": "new task title when creating",
                    "description": "new task description when creating",
                    "comment": "comment text when commenting",
                    "title_suffix": "fallback suffix under 12 words",
                    "status": "to do | in progress | for checking | complete",
                    "priority": "urgent | high | normal | low",
                    "assignee_names": ["MJ", "Troy", "Kenlie"],
                    "due_date": "YYYY-MM-DD or empty",
                }
            ],
            "dashboard": {
                "digest": {"urgent": "", "blocked": "", "watch": ""},
                "key_dates": [{"date": "", "detail": "", "priority": "normal | high | urgent"}],
                "timeline": [{"date": "", "stage": "", "detail": "", "state": "done | active | blocked | todo"}],
            },
        },
    }

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, default=str)},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
    }
    data = http_json(
        "POST",
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        payload=payload,
        timeout=90,
    )
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def assignee_ids_from_names(names: list[str] | None) -> list[int]:
    ids = []
    for name in names or []:
        mapped = ASSIGNEE_IDS.get(str(name).strip().lower())
        if mapped and mapped not in ids:
            ids.append(mapped)
    return ids


def due_date_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        date_value = dt.date.fromisoformat(value.strip())
        local_midnight = dt.datetime.combine(date_value, dt.time(23, 59), TIMEZONE)
        return int(local_midnight.timestamp() * 1000)
    except ValueError:
        return None


def signed_comment(text: str) -> str:
    text = text.strip()
    if "Codex, from Slack activity" in text:
        return text
    return f"{text}\n\n— Codex, from Slack activity"


def task_by_id(tasks: list[dict]) -> dict[str, dict]:
    return {str(task.get("id")): task for task in tasks if task.get("id")}


def update_task_title_with_suffix(task: dict, suffix: str, report: list[str]) -> None:
    suffix = suffix.strip().lstrip("-").strip()
    if not suffix:
        return
    old_name = task.get("name", "")
    if suffix.lower() in old_name.lower():
        return
    base = re.sub(r"\s+-\s+[^-]*(?:via Slack|from Slack|Slack \d{1,2} \w+).*$", "", old_name, flags=re.I)
    new_name = f"{base} - {suffix}"
    clickup_request("PUT", f"task/{task['id']}", {"name": new_name})
    task["name"] = new_name
    report.append(f"title fallback: {task['id']} {new_name}")


def apply_clickup_actions(plan: dict, tasks: list[dict]) -> list[str]:
    report: list[str] = []
    by_id = task_by_id(tasks)

    for action in plan.get("clickup_actions", []) or []:
        action_type = str(action.get("type", "")).strip().lower()
        if action_type == "create_task":
            title = str(action.get("title", "")).strip()
            if not title or looks_like_existing_task(title, tasks):
                continue
            description = str(action.get("description", "")).strip()
            if "_Created by Codex from Slack activity._" not in description:
                description = f"{description}\n\n_Created by Codex from Slack activity._".strip()
            payload: dict = {
                "name": title,
                "description": description,
                "status": action.get("status") if action.get("status") in ALLOWED_STATUSES else "to do",
            }
            priority = CLICKUP_PRIORITY.get(str(action.get("priority", "normal")).lower())
            if priority:
                payload["priority"] = priority
            assignees = assignee_ids_from_names(action.get("assignee_names"))
            if assignees:
                payload["assignees"] = assignees
            due_ms = due_date_to_ms(action.get("due_date"))
            if due_ms:
                payload["due_date"] = due_ms
            created = clickup_request("POST", f"list/{CLICKUP_LIST_ID}/task", payload)
            report.append(f"created: {created.get('id')} {title}")
            continue

        task_id = str(action.get("task_id", "")).strip()
        task = by_id.get(task_id)
        if not task:
            continue

        if action_type == "comment_task":
            comment = str(action.get("comment", "")).strip()
            if not comment:
                continue
            try:
                clickup_request("POST", f"task/{task_id}/comment", {"comment_text": signed_comment(comment), "notify_all": False})
                report.append(f"commented: {task_id}")
            except Exception as exc:
                suffix = str(action.get("title_suffix", "")).strip() or "updated via Slack"
                report.append(f"comment failed for {task_id}: {exc}")
                update_task_title_with_suffix(task, suffix, report)
            continue

        if action_type == "update_task":
            payload = {}
            status = str(action.get("status", "")).strip().lower()
            if status in ALLOWED_STATUSES:
                payload["status"] = status
            priority = CLICKUP_PRIORITY.get(str(action.get("priority", "")).lower())
            if priority:
                payload["priority"] = priority
            due_ms = due_date_to_ms(action.get("due_date"))
            if due_ms:
                payload["due_date"] = due_ms
            assignees = assignee_ids_from_names(action.get("assignee_names"))
            if assignees:
                payload["assignees"] = {"add": assignees}
            if payload:
                clickup_request("PUT", f"task/{task_id}", payload)
                report.append(f"updated: {task_id} {', '.join(payload.keys())}")

    return report


def is_open(task: dict) -> bool:
    return task_status(task) not in {"complete", "not applicable"}


def task_link(task: dict) -> str:
    task_id = html.escape(str(task.get("id", "")))
    name = html.escape(str(task.get("name", "Untitled task")))
    who = html.escape(assignee_label(task))
    return f'<a href="{CLICKUP_TASK_URL}{task_id}" target="_blank">{name}</a> <span class="who">· {who}</span>'


def dot(priority: str = "normal") -> str:
    css_class = priority if priority in {"urgent", "high", "review", "progress"} else "normal"
    return f'<span class="dot {css_class}"></span>'


def list_items(tasks: list[dict], empty: str = "Nothing here right now.") -> str:
    if not tasks:
        return f'<li>{dot("normal")}<span>{html.escape(empty)}</span></li>'
    return "\n".join(
        f'      <li>{dot("progress" if task_status(task) == "in progress" else task_priority(task))}<span>{task_link(task)}</span></li>'
        for task in tasks
    )


def word_limited(value: str, fallback: str) -> str:
    text = re.sub(r"\s+", " ", str(value or fallback)).strip()
    words = text.split()
    return " ".join(words[:24])


def fallback_digest(open_tasks: list[dict], urgent_tasks: list[dict], review_tasks: list[dict]) -> dict[str, str]:
    urgent = urgent_tasks[0]["name"] if urgent_tasks else (open_tasks[0]["name"] if open_tasks else "No urgent open task.")
    blocked = next((task["name"] for task in open_tasks if re.search(r"blocked|wait|waiting|needs|input", task.get("name", ""), re.I)), "No clear blocker in ClickUp.")
    watch = review_tasks[0]["name"] if review_tasks else (open_tasks[-1]["name"] if open_tasks else "No slipping item flagged.")
    return {
        "urgent": f"Focus on {urgent}.",
        "blocked": f"Main bottleneck: {blocked}.",
        "watch": f"Watch {watch}.",
    }


def phase_for_task(task: dict, phase_by_id: dict[str, str]) -> str:
    task_id = str(task.get("id", ""))
    parent = str(task.get("parent", "") or "")
    name = task.get("name", "")
    lower = name.lower()

    if task_id in phase_by_id:
        return phase_by_id[task_id]
    if parent in phase_by_id:
        return phase_by_id[parent]
    if "phase 0" in lower or re.search(r"deposit|kickoff|slack channel|google drive folder|project start|business data", lower):
        return "Phase 0 - Sale & Kickoff Gate"
    if "phase 1" in lower or re.search(r"onboarding|blueprint|brand|visual|pre-intake|ahpra|tga|copywriting|social media|gbp|favourite|look and feel|foundational|template", lower):
        return "Phase 1 - Onboarding"
    if "phase 3" in lower or re.search(r"subdomain|dmarc|spf|domain|email", lower):
        return "Phase 3 - Domain, Email & Technical Setup"
    if "phase 2" in lower or re.search(r"website|build|ghl|blog|menu|page|funnel|form|image|meta|heading|footer|privacy|terms|booking|service|mobile|nav|universal", lower):
        return "Phase 2 - Design & Build"
    return "Other completed"


def completed_phase_sections(completed_tasks: list[dict], all_tasks: list[dict]) -> str:
    phase_names = [
        "Phase 0 - Sale & Kickoff Gate",
        "Phase 1 - Onboarding",
        "Phase 2 - Design & Build",
        "Phase 3 - Domain, Email & Technical Setup",
        "Other completed",
    ]
    phase_by_id = {}
    for task in all_tasks:
        name = task.get("name", "").lower()
        if "phase 0" in name:
            phase_by_id[str(task.get("id"))] = phase_names[0]
        elif "phase 1" in name:
            phase_by_id[str(task.get("id"))] = phase_names[1]
        elif "phase 2" in name:
            phase_by_id[str(task.get("id"))] = phase_names[2]
        elif "phase 3" in name:
            phase_by_id[str(task.get("id"))] = phase_names[3]

    grouped = {phase: [] for phase in phase_names}
    for task in completed_tasks:
        grouped[phase_for_task(task, phase_by_id)].append(task)

    sections = []
    for phase in phase_names:
        tasks = grouped[phase]
        if not tasks and phase == "Other completed":
            continue
        items = "\n".join(f'      <li>{dot("progress")}<span>{task_link(task)}</span></li>' for task in tasks)
        sections.append(
            f'''  <details class="phase-group">
    <summary><span class="phase-title">{html.escape(phase)}</span><span class="count">{len(tasks)}</span></summary>
    <ul>
{items or '      <li><span>No completed tasks in this phase yet.</span></li>'}
    </ul>
  </details>'''
        )
    return "\n\n".join(sections)


def format_updated(now: dt.datetime) -> str:
    hour = now.strftime("%I").lstrip("0") or "0"
    return f"{now.strftime('%d %B %Y')} {hour}:{now.strftime('%M%p')} PST"


def render_dashboard(tasks: list[dict], plan: dict, now: dt.datetime, slack_window: str) -> str:
    open_tasks = [task for task in tasks if is_open(task)]
    urgent_tasks = [task for task in open_tasks if task_priority(task) in {"urgent", "high"}]
    review_tasks = [task for task in open_tasks if task_status(task) == "for checking"]
    progress_tasks = [task for task in open_tasks if task_status(task) == "in progress"]
    completed_tasks = [task for task in tasks if task_status(task) == "complete"]

    seven_days_ago = now - dt.timedelta(days=7)
    done_last_7 = 0
    for task in completed_tasks:
        done_time = timestamp_ms_to_dt(task.get("date_done")) or timestamp_ms_to_dt(task.get("date_closed")) or timestamp_ms_to_dt(task.get("date_updated"))
        if done_time and done_time >= seven_days_ago:
            done_last_7 += 1

    dashboard = plan.get("dashboard", {}) if isinstance(plan, dict) else {}
    digest = dashboard.get("digest") or fallback_digest(open_tasks, urgent_tasks, review_tasks)
    digest = {
        "urgent": word_limited(digest.get("urgent"), "No urgent item flagged."),
        "blocked": word_limited(digest.get("blocked"), "No clear blocker flagged."),
        "watch": word_limited(digest.get("watch"), "No slipping item flagged."),
    }

    key_dates = dashboard.get("key_dates") or []
    if not key_dates:
        for task in open_tasks:
            due = timestamp_ms_to_dt(task.get("due_date"))
            if due:
                key_dates.append({"date": due.strftime("%d %b %Y"), "detail": task.get("name"), "priority": task_priority(task)})
            if len(key_dates) >= 3:
                break
    if not key_dates:
        key_dates = [{"date": "Slack window", "detail": f"Latest sync read the last {slack_window}.", "priority": "normal"}]

    timeline = dashboard.get("timeline") or [
        {"date": "Completed", "stage": "Kickoff and onboarding", "detail": "Phase 0 and Phase 1 completed tasks remain archived below.", "state": "done"},
        {"date": "Now", "stage": "Build and QA", "detail": "Open build, review, and launch tasks are refreshed from ClickUp.", "state": "active"},
        {"date": "Next", "stage": "Review and launch", "detail": "Launch risk is driven by blockers, review items, and urgent tasks.", "state": "todo"},
    ]

    timeline_html = "\n".join(
        f'''    <div class="step {html.escape(str(item.get("state", "todo")))}">
      <div class="marker"></div>
      <div class="date">{html.escape(str(item.get("date", "")))}</div>
      <div class="stage">{html.escape(str(item.get("stage", "")))}</div>
      <div class="detail">{html.escape(str(item.get("detail", "")))}</div>
    </div>'''
        for item in timeline[:6]
    )

    key_dates_html = "\n".join(
        f'      <li>{dot(str(item.get("priority", "normal")))}<span><b>{html.escape(str(item.get("date", "")))}</b> <span class="meta">- {html.escape(str(item.get("detail", "")))}</span></span></li>'
        for item in key_dates[:6]
    )

    blocked_tasks = [
        task
        for task in open_tasks
        if re.search(r"blocked|waiting|wait|needs|input|approve|approval", task.get("name", ""), re.I)
    ][:12]

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Brandwithin — Client Projects</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f7f6f3; color: #1f2430; padding: 28px; font-size: 15px; max-width: 1120px; margin: 0 auto; }}
  header {{ margin-bottom: 6px; }}
  h1 {{ font-size: 24px; font-weight: 700; }}
  .sub {{ color: #8a8577; font-size: 13px; margin-bottom: 22px; }}
  h2.client {{ font-size: 18px; margin: 10px 0 14px; }}
  h2.client span {{ color: #8a8577; font-weight: 500; font-size: 14px; }}
  .chips {{ display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 20px; }}
  .chip {{ background: #fff; border: 1px solid #e6e2d8; border-radius: 10px; padding: 12px 18px; min-width: 112px; }}
  .chip .num {{ font-size: 24px; font-weight: 700; }}
  .chip .lbl {{ font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: #8a8577; margin-top: 2px; }}
  .red .num {{ color: #c0392b; }}
  .amber .num {{ color: #b8860b; }}
  .blue .num {{ color: #2563ab; }}
  .green .num {{ color: #2e7d32; }}
  .digest {{ background: #fdf9ec; border: 1px solid #ede3bd; border-radius: 12px; padding: 16px 18px; margin-bottom: 20px; line-height: 1.6; }}
  .digest h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #8a6d1f; margin-bottom: 8px; }}
  .digest b {{ display: inline-block; min-width: 74px; }}
  .timeline {{ background: #fff; border: 1px solid #e6e2d8; border-radius: 12px; padding: 18px; margin-bottom: 20px; }}
  .timeline h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #6b6555; margin-bottom: 16px; display: flex; justify-content: space-between; gap: 12px; }}
  .track {{ display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 0; overflow-x: auto; padding-bottom: 2px; }}
  .step {{ position: relative; min-width: 120px; padding: 0 14px 0 0; }}
  .step:before {{ content: ""; position: absolute; top: 8px; left: 12px; right: 0; height: 2px; background: #e7e1d6; }}
  .step:last-child:before {{ display: none; }}
  .marker {{ position: relative; width: 18px; height: 18px; border-radius: 50%; background: #fff; border: 2px solid #b9b3a2; margin-bottom: 10px; z-index: 1; }}
  .step.done .marker {{ background: #2e7d32; border-color: #2e7d32; }}
  .step.active .marker {{ background: #2563ab; border-color: #2563ab; }}
  .step.blocked .marker {{ background: #c0392b; border-color: #c0392b; }}
  .date {{ color: #8a8577; font-size: 12px; margin-bottom: 3px; }}
  .stage {{ font-weight: 650; line-height: 1.3; margin-bottom: 4px; }}
  .detail {{ color: #6f6a5d; font-size: 12.5px; line-height: 1.35; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .card {{ background: #fff; border: 1px solid #e6e2d8; border-radius: 12px; padding: 16px 18px; }}
  .completed {{ margin-bottom: 20px; }}
  .completed h3 {{ align-items: center; }}
  .completed ul {{ columns: 2 320px; column-gap: 28px; }}
  .completed li {{ break-inside: avoid; }}
  .phase-group {{ border-top: 1px solid #f0ede4; padding: 8px 0; }}
  .phase-group:first-of-type {{ border-top: none; }}
  .phase-group summary {{ cursor: pointer; list-style: none; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 5px 0; }}
  .phase-group summary::-webkit-details-marker {{ display: none; }}
  .phase-title {{ font-weight: 650; }}
  .phase-title:before {{ content: "+"; color: #8a8577; display: inline-block; width: 16px; }}
  .phase-group[open] .phase-title:before {{ content: "-"; }}
  .phase-group ul {{ margin-top: 6px; padding-left: 16px; }}
  .phase-group li:first-child {{ border-top: 1px solid #f0ede4; }}
  .card h3 {{ font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: #6b6555; margin-bottom: 10px; display: flex; justify-content: space-between; gap: 12px; }}
  .count {{ background: #f0ede4; border-radius: 10px; padding: 1px 9px; font-size: 12px; color: #1f2430; }}
  ul {{ list-style: none; }}
  li {{ padding: 8px 0; border-top: 1px solid #f0ede4; display: flex; gap: 9px; align-items: flex-start; line-height: 1.45; }}
  li:first-child {{ border-top: none; }}
  .dot {{ width: 8px; height: 8px; border-radius: 50%; margin-top: 6px; flex-shrink: 0; }}
  .dot.urgent {{ background: #c0392b; }}
  .dot.high {{ background: #e67e22; }}
  .dot.normal {{ background: #b9b3a2; }}
  .dot.review {{ background: #d4a017; }}
  .dot.progress {{ background: #2563ab; }}
  a {{ color: #1f2430; }}
  a:hover {{ color: #2563ab; }}
  .who {{ color: #8a8577; font-size: 12.5px; white-space: nowrap; }}
  .meta {{ color: #6f6a5d; font-size: 12.5px; display: inline; }}
  footer {{ color: #b0aa99; font-size: 12px; margin-top: 26px; text-align: center; }}
  @media (max-width: 720px) {{
    body {{ padding: 18px; }}
    .chip {{ min-width: calc(50% - 5px); }}
    .track {{ grid-template-columns: 1fr; gap: 14px; overflow: visible; }}
    .step {{ padding-left: 28px; min-width: 0; }}
    .step:before {{ top: 18px; left: 8px; right: auto; bottom: -18px; width: 2px; height: auto; }}
    .step:last-child:before {{ display: none; }}
    .marker {{ position: absolute; left: 0; top: 0; }}
    .completed ul {{ columns: 1; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Brandwithin — Client Projects</h1>
</header>
<div class="sub">Updated {html.escape(format_updated(now))} · refreshed automatically from ClickUp + Slack</div>

<h2 class="client">Mariangela Parodi <span>· Website Build</span></h2>

<div class="chips">
  <div class="chip"><div class="num">{len(open_tasks)}</div><div class="lbl">Open tasks</div></div>
  <div class="chip red"><div class="num">{len(urgent_tasks)}</div><div class="lbl">Urgent / High</div></div>
  <div class="chip amber"><div class="num">{len(review_tasks)}</div><div class="lbl">Waiting review</div></div>
  <div class="chip blue"><div class="num">{len(progress_tasks)}</div><div class="lbl">In progress</div></div>
  <div class="chip green"><div class="num">{done_last_7}</div><div class="lbl">Done last 7 days</div></div>
</div>

<div class="digest">
  <h3>Morning digest</h3>
  <div><b>URGENT:</b> {html.escape(digest["urgent"])}</div>
  <div><b>BLOCKED:</b> {html.escape(digest["blocked"])}</div>
  <div><b>WATCH:</b> {html.escape(digest["watch"])}</div>
</div>

<section class="timeline">
  <h3>Project timeline <span>Design → Review → Launch</span></h3>
  <div class="track">
{timeline_html}
  </div>
</section>

<div class="grid">
  <div class="card">
    <h3>🔥 Urgent &amp; high priority <span class="count">{len(urgent_tasks)}</span></h3>
    <ul>
{list_items(urgent_tasks[:12], "No urgent or high-priority tasks right now.")}
    </ul>
  </div>

  <div class="card">
    <h3>⛔ Blocked <span class="count">{len(blocked_tasks)}</span></h3>
    <ul>
{list_items(blocked_tasks, "No blocked task is clearly flagged right now.")}
    </ul>
  </div>

  <div class="card">
    <h3>👀 Waiting on review <span class="count">{len(review_tasks)}</span></h3>
    <ul>
{list_items(review_tasks[:12], "Nothing is waiting for review right now.")}
    </ul>
  </div>

  <div class="card">
    <h3>🚧 In progress <span class="count">{len(progress_tasks)}</span></h3>
    <ul>
{list_items(progress_tasks[:12], "No task is marked in progress right now.")}
    </ul>
  </div>

  <div class="card">
    <h3>📅 Key dates <span class="count">{len(key_dates[:6])}</span></h3>
    <ul>
{key_dates_html}
    </ul>
  </div>
</div>

<section class="card completed">
  <h3>✅ Completed tasks <span class="count">{len(completed_tasks)}</span></h3>

{completed_phase_sections(completed_tasks, tasks)}
</section>

<footer>Generated automatically from ClickUp + Slack · Brandwithin internal use</footer>
</body>
</html>
'''


def main() -> int:
    now = dt.datetime.now(TIMEZONE)
    slack_messages, slack_window = fetch_slack_messages(now)
    tasks = fetch_clickup_tasks()

    plan = {"clickup_actions": [], "dashboard": {}}
    try:
        plan = ask_ai_for_plan(slack_messages, tasks, now)
    except Exception as exc:
        print(f"AI planning failed; using read-only dashboard fallback: {exc}", file=sys.stderr)

    action_report = []
    if slack_messages and plan.get("clickup_actions"):
        action_report = apply_clickup_actions(plan, tasks)
        tasks = fetch_clickup_tasks()

    html_output = render_dashboard(tasks, plan, now, slack_window)
    Path("index.html").write_text(html_output, encoding="utf-8")

    print(f"Slack window used: {slack_window}")
    print(f"Slack messages read: {len(slack_messages)}")
    print(f"ClickUp tasks read: {len(tasks)}")
    if action_report:
        print("ClickUp changes:")
        for item in action_report:
            print(f"- {item}")
    else:
        print("ClickUp changes: none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
