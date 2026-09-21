# Discord 一斉メンション送信 Bot (/broadcast)

サーバーのメンバーへメンションを付けながら、任意の文面を任意の回数送る Discord Bot (スラッシュコマンド) です。

- @everyone を抑制しているメンバーにも通知を届けたいときは target に members を指定します。メンバーを 1 人ずつ <@id> 形式で個別メンションするため、@everyone の通知設定に関係なく通知が飛びます。
- 送信回数は count で指定 (既定 1、最大 20。上限は環境変数 MAX_COUNT で変更可)。
- 送信前に確認ボタン (送信する / キャンセル) が表示されます。確認メッセージは実行した本人だけに見えるので、チャンネルを汚しません。
- メンバーが多い場合は 1 メッセージ 2000 文字の制限に収まるよう、自動で複数メッセージに分割して送信します。

## 1. Bot を作る

1. https://discord.com/developers/applications を開き、New Application でアプリを作成します。
2. 左メニューの Bot タブでトークンを発行 (Reset Token) して控えます。このトークンは絶対に他人へ見せないでください。
3. 同じページの Privileged Gateway Intents で **SERVER MEMBERS INTENT** を ON にして Save Changes します。ここが OFF だとメンバー一覧を取得できず、target=members が動きません。

## 2. サーバーに招待する

OAuth2 > URL Generator で次を選び、生成された URL から対象サーバーへ追加します。

- Scopes: bot, applications.commands
- Bot Permissions: View Channels, Send Messages, Mention Everyone

## 3. 使い方

チャンネルで /broadcast を実行すると、まず確認メッセージ (実行者だけに見える) が出ます。内容を確認して「送信する」を押すと送信が始まり、「キャンセル」を押すと何も送信しません。180 秒操作がなければ自動で失効します。

| オプション | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| message | はい | - | 送信する本文 |
| count | いいえ | 1 | 送信する回数 (1〜20) |
| target | いいえ | everyone | メンション対象: everyone / here / members / role / none |
| users | いいえ | - | 個別にメンションする相手。カンマ区切りの ID / @メンション / 名前。指定すると個別メンションに切り替わります |
| role | いいえ | - | target=role のときにメンションするロール |
| filter_role | いいえ | - | target=members のときに、このロールを持つ人だけに送る |
| send_style | いいえ | chunked | members の送り方: chunked (まとめて) / per_member (1 人ずつ) |
| delay | いいえ | 1.5 | 1 メッセージごとの待機秒数 (0〜30) |
| interval | いいえ | 0 | 回と回の間隔 (秒、0〜3600)。連続送信に使います |
| include_bots | いいえ | false | 対象に Bot も含める |
| dry_run | いいえ | false | 確認ボタンを出さずに内容だけ確認する |

実行例:

    /broadcast message:"メンテナンスのお知らせ" count:3 target:members
    /broadcast message:"集合！" users:"1001, @alice, ボブ"
    /broadcast message:"1人ずつお知らせ" target:members send_style:per_member count:2
    /broadcast message:"10分おきに連続" interval:600 count:6 target:members

## 個別メンションと連続送信

- 特定の相手だけに個別メンションする: users に相手を書きます。ユーザー ID / @メンション / ユーザー名 / 表示名 のどれでも指定でき、カンマ区切りで複数指定できます。users を指定すると自動で個別メンションになります。
- 1 人ずつ別々のメッセージで送る: send_style:per_member を指定します。たとえば 200 人のサーバーで count:1 なら 200 通を順番に送ります。人数分の時間がかかるため、進捗を見ながら「停止」で止められます。
- 連続送信する: interval で回と回の間隔を指定します。interval:600 count:6 なら 10 分おきに 6 回送ります。
- 送信中は実行者だけに見えるメッセージに「停止」ボタンが出ます。押すとその回で打ち切ります。

確認メッセージには、送信先チャンネル・メンション方式・対象人数・送信回数・合計メッセージ数・本文・メンションの例 (先頭 5 人) が表示されます。確認メッセージ内のメンションは通知されません。

送信に 10 分以上かかる見込みのときは、確認メッセージに推定所要時間が表示されます。長い処理では進捗表示が途中で止まることがありますが、送信自体は最後まで続きます。

## 4. ローカルで動かす

PowerShell でこのフォルダに移動してから実行します。

    python -m venv .venv
    .venv/Scripts/Activate.ps1
    pip install -r requirements.txt
    $env:DISCORD_TOKEN = "ここにトークン"
    $env:ALLOWED_GUILD_IDS = "サーバーID"   # 省略可 (即時反映したいとき)
    python bot.py

起動すると自動でコマンドが登録されます。ALLOWED_GUILD_IDS を指定しない場合はグローバル登録になり、反映まで最大 1 時間かかります。PC を閉じれば Bot は停止します。

## 5. Render にデプロイする (24 時間動かす場合)

Bot は Discord と常時接続 (Gateway / WebSocket) してスラッシュコマンドを受け取るため、動かし続ける場所が必要です。Render が使えるなら同梱の render.yaml でそのままデプロイできます。

手順:

1. このフォルダ (bot.py, requirements.txt, render.yaml) を GitHub リポジトリに入れます。
2. Render のダッシュボードで New > Blueprint を選び、そのリポジトリを指定します。
3. 環境変数 DISCORD_TOKEN (必須) と ALLOWED_GUILD_IDS (任意) を入力します。
4. デプロイが完了すると Bot が起動し、/health に 200 を返します。

補足:

- PORT 環境変数が設定されていると bot.py は自動で /health 用の小さな HTTP サーバーを同じプロセス内で起動します (Render の Web Service はポートを開かないとデプロイが失敗扱いになるため)。ローカル実行時は PORT がないので HTTP サーバーは立ちません。
- Render の無料 Web Service は 15 分アクセスがないとスリープし、その間 Bot はオフラインになります。常時起動したい場合は次のどちらかにします。
  - Render の Background Worker (type: worker、有料) に変える。render.yaml の type を worker にし、healthCheckPath を消すだけです。
  - UptimeRobot などで /health を 10 分間隔で叩き、スリープさせない。
- 参考までに、他にも Railway / Fly.io / Oracle Cloud 無料枠 / VPS (月 500 円程度) / 自宅 PC で動かす方法があります。小規模で使うだけならローカル実行でも十分です。
- interval を使った長時間の連続送信は、Free プランのスリープ (15 分無通信で停止) に引っかかると途中で止まります。長い間隔で回す場合は Starter プランか、外部から定期的に /health を叩く運用にしてください。

## 6. 通知が届く条件

- 個別メンション (target=members) は、ユーザー設定の「@everyone と @here の通知を抑制」が ON でも通知されます。
- 届かないケース: チャンネル個別ミュート、サーバーミュート、通知の集中モード (DND) 中、そのユーザーが既にサーバーを退出している場合。
- メンバー数が多いほど、1 回の送信で複数メッセージに分かれます。たとえば 200 人のサーバーなら 1 回あたり 3 メッセージ前後になります。

## 7. 注意点

- 自分のサーバー、または管理者から許可を得たサーバーでのみ使ってください。無関係なサーバーへの一斉メンションはスパム扱いとなり、アカウント停止の対象になります。
- 大量メンションは Discord のレート制限に当たります。delay は 1 秒以上を推奨します (429 応答時は discord.py が自動で待機します)。
- トークンが漏れると第三者に Bot を操作されます。Git にコミットしたり配布したりしないでください。Render では環境変数として設定し、リポジトリには書かないでください。
- メンバーへ DM で直接通知する方法もありますが、スパム判定リスクが高く、受け取る側の拒否も難しいためこの Bot では実装していません。
