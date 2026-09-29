# 北海道日豪協会だより（LINE週刊配信）

北海道日豪協会の会員向けに、オーストラリアの話題を毎週土曜の朝、LINEグループへ届ける仕組みです。
4週で4つのテーマ（歴史秘話／自然・ガーデニング／北海道との接点／生活・食文化）を順番に扱います。

- 原稿作成：Anthropic API（Web検索で事実確認しながら執筆）
- 配信：LINE Messaging API（公式アカウントからグループへプッシュ送信）
- 起動：GitHub Actions（毎週土曜 7:45 日本時間）

| ファイル | 役割 |
|---|---|
| `newsletter/prompt_spec.md` | 執筆仕様書（文体・分量・テーマ候補・事実確認のルール）。内容を変えたいときはここを編集 |
| `newsletter/state.json` | 次に配信する週と、配信済みの題材の記録 |
| `newsletter/generate_and_send.py` | 原稿作成とLINE送信のプログラム |
| `.github/workflows/weekly-line-newsletter.yml` | 定時実行の設定 |
| `archive/` | 送信した原稿の控え |
| `tools/line_group_id_webhook.gs` | グループIDを調べるための一時的なWebhook |

設定の手順は [docs/SETUP.md](docs/SETUP.md) を参照してください。
