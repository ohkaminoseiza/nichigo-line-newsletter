/**
 * LINE グループID確認用の一時的な Webhook（Google Apps Script）
 *
 * 使い方（詳しくは docs/SETUP.md の手順3）
 *   1. script.google.com で新しいプロジェクトを作り、このコードを貼り付ける
 *   2. 下の CHANNEL_ACCESS_TOKEN に、チャネルアクセストークン（長期）を貼り付ける
 *   3. 「デプロイ」→「新しいデプロイ」→ 種類「ウェブアプリ」
 *      実行ユーザー：自分 ／ アクセスできるユーザー：全員 でデプロイし、表示されたURLをコピー
 *   4. LINE Developers の Messaging API 設定で、Webhook URL に貼り付けて「Webhookの利用」をオン
 *   5. 公式アカウントをLINEグループに招待すると、そのグループにグループIDが返信される
 *      （あとから知りたいときは、グループで「ID」と送信）
 *   6. グループIDを控えたら、Webhook の利用をオフにし、このデプロイは削除してかまいません
 *
 * 返信（リプライ）はメッセージ通数にカウントされません。
 */

const CHANNEL_ACCESS_TOKEN = 'ここにチャネルアクセストークン（長期）を貼り付け';

function doPost(e) {
  const body = JSON.parse(e.postData.contents);
  (body.events || []).forEach(function (ev) {
    if (!ev.replyToken) return;
    const isJoin = ev.type === 'join';
    const asked = ev.type === 'message' && ev.message && ev.message.type === 'text' &&
      /^(ID|ＩＤ|グループID|グループＩＤ)$/i.test(String(ev.message.text).trim());
    if (!isJoin && !asked) return;

    const src = ev.source || {};
    let text;
    if (src.type === 'group') {
      text = 'このグループのIDは次のとおりです。\n\n' + src.groupId +
        '\n\nGitHub の Secrets「LINE_GROUP_ID」に登録してください。';
    } else if (src.type === 'room') {
      text = 'このトークルームのIDは次のとおりです。\n\n' + src.roomId;
    } else {
      text = 'あなたのユーザーIDは次のとおりです。\n\n' + src.userId;
    }

    UrlFetchApp.fetch('https://api.line.me/v2/bot/message/reply', {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + CHANNEL_ACCESS_TOKEN },
      payload: JSON.stringify({ replyToken: ev.replyToken, messages: [{ type: 'text', text: text }] }),
      muteHttpExceptions: true,
    });
  });
  return ContentService.createTextOutput('OK');
}
