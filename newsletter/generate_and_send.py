#!/usr/bin/env python3
"""北海道日豪協会だより：原稿を Anthropic API で作成し、LINE グループへプッシュ送信する。

GitHub Actions から毎週1回実行される想定。手元で試すときは:

    ANTHROPIC_API_KEY=... python newsletter/generate_and_send.py --dry-run

環境変数
    ANTHROPIC_API_KEY          Anthropic API キー（必須）
    LINE_CHANNEL_ACCESS_TOKEN  LINE チャネルアクセストークン（長期）（送信時に必須）
    LINE_GROUP_ID              送信先のグループID（C で始まる文字列）（送信時に必須）
    NEWSLETTER_MODEL           使用モデル（省略時 claude-sonnet-5）
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "newsletter" / "prompt_spec.md"
STATE_PATH = ROOT / "newsletter" / "state.json"
ARCHIVE_DIR = ROOT / "archive"

JST = dt.timezone(dt.timedelta(hours=9))
TITLE_PREFIX = "【北海道日豪協会だより】"
DEFAULT_MODEL = "claude-sonnet-5"
MAX_SEARCHES = 8

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_TEXT_LIMIT = 5000      # テキストメッセージ1件あたりの上限文字数
LINE_MAX_MESSAGES = 5       # 1リクエストで送れるメッセージ数の上限

BODY_MIN, BODY_MAX = 700, 1300          # これを外れたら書き直しを依頼する
WEATHER_WORDS = [
    "秋が深", "秋も深", "冷え込", "肌寒", "紅葉", "雪の便り", "初雪", "寒さが",
    "暑さが", "残暑", "梅雨", "桜の", "春めい", "暖かくなって", "寒くなって",
    "日差しが", "朝晩の",
]

OUTPUT_FORMAT = """\
## 出力形式（厳守）

調べ終えたら、最後に次の形式だけで原稿を出力してください。区切り行（===で囲んだ行）はそのまま書いてください。

===TITLE===
（「【北海道日豪協会だより】」の後に続く一言。20〜35字程度。【】は付けない）
===TOPIC===
（配信記録用の題材名。20字程度。例：木曜島の日本人真珠貝ダイバー）
===BODY===
（本文。1000字程度。見出し・Markdown・絵文字は使わない）
===SOURCES===
1. 「資料名」／発行機関／URL／公開年
2. …
===END===
"""


# ---------------------------------------------------------------- 状態ファイル

def load_state() -> dict:
    with STATE_PATH.open(encoding="utf-8") as f:
        state = json.load(f)
    state.setdefault("next_week", 1)
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def next_week_of(week: int) -> int:
    return week % 4 + 1


# ---------------------------------------------------------------- 仕様書

def parse_week_sections(spec: str) -> dict[int, dict]:
    """仕様書の「### 第N週」ブロックを取り出す。"""
    sections: dict[int, dict] = {}
    matches = list(re.finditer(r"^### 第(\d)週\s*$", spec, flags=re.M))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(spec)
        block = spec[m.end():end].strip()
        # 次の「## 」見出し以降は含めない
        block = re.split(r"^## ", block, flags=re.M)[0].strip()
        theme = re.search(r"テーマ名[：:]\s*(.+)", block)
        sections[int(m.group(1))] = {
            "theme": theme.group(1).strip() if theme else f"第{m.group(1)}週",
            "block": block,
        }
    missing = {1, 2, 3, 4} - set(sections)
    if missing:
        raise SystemExit(f"仕様書に第{sorted(missing)}週の定義がありません: {SPEC_PATH}")
    return sections


def common_spec(spec: str) -> str:
    """「## 週ごとのテーマ」より前の共通ルール部分。"""
    return re.split(r"^## 週ごとのテーマ", spec, flags=re.M)[0].strip()


def build_prompts(spec: str, week: int, section: dict, history: list[dict], today: dt.date):
    system = common_spec(spec) + "\n\n" + OUTPUT_FORMAT
    used = [h["topic"] for h in history if h.get("topic")]
    used_text = "\n".join(f"- {t}" for t in used) if used else "（まだありません）"
    user = f"""今日は{today.year}年{today.month}月{today.day}日です。
今回は第{week}週「{section['theme']}」の回です。

【今週のテーマの指示】
{section['block']}

【これまでに配信した題材（重複禁止）】
{used_text}

上の候補から、これまでに配信した題材と重ならないものを1つ選んでください。
Web検索で複数の信頼できる情報源を確認し、事実確認のルールを守って原稿を書いてください。
参照資料の閲覧年月は「{today.year}年{today.month}月閲覧」としてください。
最後に、指定の出力形式で原稿だけを出力してください。"""
    return system, user


# ---------------------------------------------------------------- Claude 呼び出し

def run_turns(client, model: str, system: str, messages: list) -> str:
    """1往復分を実行し（pause_turn なら続行）、新たに得たテキストを返す。"""
    tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_SEARCHES}]
    texts: list[str] = []
    for _ in range(6):
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        texts.extend(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason == "pause_turn":
            continue  # 長い検索の途中で一時停止した場合は、そのまま続けてもらう
        break
    return "".join(texts)


def section_between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i) if end else -1
    return (text[i:j] if j >= 0 else text[i:]).strip()


def parse_output(raw: str) -> dict:
    # 調査メモなどが前に付いても、最後の ===TITLE=== 以降だけを読む
    k = raw.rfind("===TITLE===")
    text = raw[k:] if k >= 0 else raw
    d = {
        "title": section_between(text, "===TITLE===", "===TOPIC==="),
        "topic": section_between(text, "===TOPIC===", "===BODY==="),
        "body": section_between(text, "===BODY===", "===SOURCES==="),
        "sources": section_between(text, "===SOURCES===", "===END==="),
    }
    return clean(d)


def clean(d: dict) -> dict:
    title = d["title"].strip().strip("「」")
    if title.startswith(TITLE_PREFIX):
        title = title[len(TITLE_PREFIX):].strip()
    d["title"] = title.splitlines()[0].strip() if title else ""
    d["topic"] = d["topic"].splitlines()[0].strip() if d["topic"] else ""
    # 念のため Markdown の強調記号を除去し、空行を整える
    body = d["body"].replace("**", "")
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    d["body"] = body
    src = d["sources"].replace("**", "").strip()
    src = re.sub(r"^【参照資料】\s*", "", src)
    d["sources"] = re.sub(r"\n{2,}", "\n", src).strip()
    return d


def char_count(s: str) -> int:
    return len(re.sub(r"\s", "", s))


def validate(d: dict, history: list[dict]) -> tuple[list[str], list[str]]:
    """(致命的な問題, 書き直しで直したい問題) を返す。"""
    fatal, soft = [], []
    if not d["title"]:
        fatal.append("タイトル（===TITLE===）がありません")
    if not d["body"]:
        fatal.append("本文（===BODY===）がありません")
    if not re.search(r"https?://", d["sources"]):
        fatal.append("参照資料にURLがありません")
    if fatal:
        return fatal, soft

    n = char_count(d["body"])
    if not BODY_MIN <= n <= BODY_MAX:
        soft.append(f"本文が{n}字です。800〜1100字程度に調整してください")
    heading_lines = [ln for ln in d["body"].splitlines() if ln.strip().startswith("【")]
    if heading_lines:
        soft.append("本文に【　】の見出しがあります。見出しを削除し地の文でつないでください: "
                    + " / ".join(heading_lines[:3]))
    md_lines = [ln for ln in d["body"].splitlines() if re.match(r"\s*(#|- |\* |\d+\. )", ln)]
    if md_lines:
        soft.append("本文に見出し記号や箇条書きがあります。地の文にしてください")
    head, tail = d["body"][:150], d["body"][-150:]
    hits = [w for w in WEATHER_WORDS if w in head or w in tail]
    if hits:
        soft.append("冒頭または結びのあいさつに天気・季節の表現があります（"
                    + "、".join(hits) + "）。削除してください")
    used = {h.get("topic", "") for h in history}
    if d["topic"] and d["topic"] in used:
        soft.append(f"題材「{d['topic']}」は配信済みです。別の題材にしてください")
    return fatal, soft


def generate(week: int, section: dict, spec: str, history: list[dict], model: str,
             today: dt.date) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    system, user = build_prompts(spec, week, section, history, today)
    messages: list = [{"role": "user", "content": user}]

    raw = run_turns(client, model, system, messages)
    d = parse_output(raw)
    fatal, soft = validate(d, history)

    if fatal or soft:
        problems = fatal + soft
        print("書き直しを依頼します: " + " / ".join(problems), file=sys.stderr)
        messages.append({"role": "user", "content":
                         "次の点を直して、指定の出力形式で原稿全体をもう一度出力してください。\n- "
                         + "\n- ".join(problems)})
        raw = run_turns(client, model, system, messages)
        d = parse_output(raw)
        fatal, soft = validate(d, history)

    if fatal:
        raise SystemExit("原稿を作成できませんでした: " + " / ".join(fatal) + "\n--- 出力 ---\n" + raw)
    d["warnings"] = soft
    return d


# ---------------------------------------------------------------- LINE 送信

def compose_line_text(d: dict) -> str:
    return (f"{TITLE_PREFIX}{d['title']}\n\n"
            f"{d['body']}\n\n"
            f"【参照資料】\n{d['sources']}")


def split_for_line(text: str) -> list[str]:
    """5000字を超える場合は段落の切れ目で分割する（最大5件）。"""
    if len(text) <= LINE_TEXT_LIMIT:
        return [text]
    parts, cur = [], ""
    for para in text.split("\n\n"):
        piece = para if not cur else cur + "\n\n" + para
        if len(piece) <= LINE_TEXT_LIMIT:
            cur = piece
        else:
            if cur:
                parts.append(cur)
            while len(para) > LINE_TEXT_LIMIT:
                parts.append(para[:LINE_TEXT_LIMIT])
                para = para[LINE_TEXT_LIMIT:]
            cur = para
    if cur:
        parts.append(cur)
    if len(parts) > LINE_MAX_MESSAGES:
        raise SystemExit(f"メッセージが長すぎます（{len(parts)}件に分割が必要）")
    return parts


def push_line(token: str, to: str, texts: list[str]) -> None:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        # 同じキーで再送すれば、二重送信にならない
        "X-Line-Retry-Key": str(uuid.uuid4()),
    }
    payload = {"to": to, "messages": [{"type": "text", "text": t} for t in texts]}
    for attempt in range(1, 4):
        try:
            r = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=30)
        except requests.RequestException as e:
            print(f"LINE送信で通信エラー（{attempt}回目）: {e}", file=sys.stderr)
        else:
            if r.status_code == 200:
                return
            if r.status_code == 409:   # 同じリトライキーで受付済み
                return
            if r.status_code not in (429, 500, 502, 503, 504):
                raise SystemExit(f"LINE送信に失敗しました: HTTP {r.status_code} {r.text}")
            print(f"LINE送信を再試行します（HTTP {r.status_code}）", file=sys.stderr)
        time.sleep(5 * attempt)
    raise SystemExit("LINE送信に3回失敗しました")


# ---------------------------------------------------------------- メイン

def write_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="原稿を作るだけで、LINE送信も記録もしない")
    ap.add_argument("--week", type=int, choices=[1, 2, 3, 4], help="週を指定（省略時は state.json の次の週）")
    args = ap.parse_args()

    today = dt.datetime.now(JST).date()
    state = load_state()
    spec = SPEC_PATH.read_text(encoding="utf-8")
    sections = parse_week_sections(spec)
    week = args.week or int(state["next_week"])
    section = sections[week]
    model = os.environ.get("NEWSLETTER_MODEL") or DEFAULT_MODEL

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY が設定されていません")
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    group = os.environ.get("LINE_GROUP_ID", "")
    if not args.dry_run and not (token and group):
        raise SystemExit("LINE_CHANNEL_ACCESS_TOKEN と LINE_GROUP_ID を設定してください")

    print(f"第{week}週「{section['theme']}」の原稿を作成します（モデル: {model}）")
    d = generate(week, section, spec, state["history"], model, today)
    text = compose_line_text(d)
    parts = split_for_line(text)

    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"本文 {char_count(d['body'])}字 / 全体 {len(text)}字 / LINEメッセージ {len(parts)}件")
    for w in d["warnings"]:
        print(f"注意: {w}", file=sys.stderr)

    mode = "プレビュー（送信なし）" if args.dry_run else "LINEへ送信"
    write_summary(f"## {mode}：第{week}週「{section['theme']}」\n\n"
                  f"- 題材：{d['topic']}\n- 本文：{char_count(d['body'])}字\n"
                  + "".join(f"- 注意：{w}\n" for w in d["warnings"])
                  + f"\n```text\n{text}\n```\n")

    if args.dry_run:
        return

    push_line(token, group, parts)
    print("LINEグループへ送信しました")

    ARCHIVE_DIR.mkdir(exist_ok=True)
    (ARCHIVE_DIR / f"{today.isoformat()}_week{week}.txt").write_text(text + "\n", encoding="utf-8")
    state["history"].append({
        "date": today.isoformat(),
        "week": week,
        "theme": section["theme"],
        "topic": d["topic"],
        "title": TITLE_PREFIX + d["title"],
        "channel": "line",
    })
    if not args.week or args.week == int(state["next_week"]):
        state["next_week"] = next_week_of(week)
    save_state(state)


if __name__ == "__main__":
    main()
