# claude-code-voice-mod

[English README](README.md)

Windows で、Claude Code のセッションに声で話しかけるための Mod です（macOS は実験的対応: 実機の Mac では未テスト）。
バージョン 0.4.0（[CHANGELOG](CHANGELOG.md)）。

Claude Code の **Mod**（関数フックで作るプラグイン）と、小さな Python ヘルパーでできています。
`codex app-server` を経由して ChatGPT のログインで使える OpenAI の realtime 音声モデルが「音声窓口」になり、
雑談には自分で答え、実際の作業（「ログを見て、ビルドが失敗する理由を教えて」など）は開いている Claude Code の
セッションに依頼します。Claude の作業が終わると、音声窓口が結果を1〜2文に要約して読み上げます。

> [!WARNING]
> ## 免責事項とリスク（最初に読んでください）
>
> - **実験的・非公開の API を使います。** `codex app-server` の *実験的な* realtime API
>   （`thread/realtime/*`、`experimentalApi: true` で有効化）を **ChatGPT プランの認証** で使います。
>   公開・文書化・サポートされたインターフェースではありません。**codex-cli 0.160.0** で動作確認しており、
>   codex の更新で予告なく動かなくなる可能性があります。
> - **非公式です。** 本プロジェクトは OpenAI とも Anthropic とも無関係で、承認・支援を受けていません。
>   「Claude」「Claude Code」「OpenAI」「ChatGPT」「Codex」は各社の商標です。codex は外部プログラムとして
>   起動しているだけで、OpenAI や Anthropic のコードは含みません。
> - **利用者の責任で使ってください。** OpenAI と Anthropic の利用規約・利用ポリシー（ChatGPT プランの使い方を含む）
>   に従っているかの確認は利用者の責任です。判断がつかない場合は使わないでください。
> - **OpenAI に送られるデータ:** マイクの音声、その文字起こし、Claude への依頼文、そして
>   **Claude の回答（約2000字まで、コードブロックは省略）** が、要約のために OpenAI へ送られます。
>   回答に含まれるファイルの中身やパスも外部に出ます。送る前に、秘密情報らしい文字列（API キー、トークン、JWT、
>   秘密鍵、`password = 1234`・`"pin": 0000`・「パスワードは…」のような秘密を表す名前の値、接続文字列の認証情報、
>   長いランダム文字列）を `[REDACTED]` に置き換えますが、**ベストエフォート** です（よくある形式しか分からず、
>   すべての秘密や個人情報は見分けられません）。マイクの音声はページから OpenAI へ直接送られ、伏せ字にはなりません。
> - **誤作動しえます。** 周りの話し声やテレビ、聞き間違いが Claude への依頼になることがあります。
>   音声の依頼は **Claude Code のセッションと同じ権限** で実行されます。
> - **ガードは補助的な安全網で、安全境界ではありません。** 音声由来のターンの危険な操作は文字列ベースのガード（後述）で
>   止めますが、簡単にすり抜けられます（例: ファイルを消すスクリプトを実行する）。既知のパターンしか止めません。
>   Claude Code の通常の権限確認はオンのままにし、ガードがあるからと自動承認を広げないでください。
> - **少人数で試すための実験版です。** 自分で使うか、上の注意点を理解した数人と試す用途に限ってください。
>   セキュリティレビューを4回受けました（[セキュリティレビュー](#セキュリティレビュー)）が、ガードには一部の
>   例外的な条件で外れる既知の制限が残っており、レビューでは実サービスとの通しの動作確認もしていません。
>   「危険な操作を確実に防げる」ものとして紹介しないでください。
> - **外部に出してはいけないデータには使わないでください。** 秘密情報・認証情報のあるフォルダや、読み上げられたり
>   OpenAI に送られたりして困るものがある場所では `/voice on` しないこと（ログをオフにしても送信内容は変わりません）。
>   ヘッドセットを使い、使わないときは `/voice off` してください。
>
> 本ソフトウェアは無保証で「現状のまま」提供されます（[LICENSE](LICENSE)）。

## デモの流れ

1. Claude Code で `/voice on docs-site` と入力。ブラウザのアプリウインドウ（「[docs-site] Voice Mod」）が開きます
   （Windows は Edge。macOS は Chrome・Edge・Brave・Chromium のいずれか、なければ既定のブラウザ）。
2. **Start (uses the microphone)** を押して話しかけます。「こんにちは」→ 音声窓口が自分で返事します。
3. 「このプロジェクトで失敗しているテストを調べて、理由を教えて」と話すと、短いひとこと（「確認しますね」など）の
   あと、依頼が Claude に渡ります。
4. Claude Code に `[voice] ...` で始まるターンが立ち、ステータス行に `voice: Claude working on "..."` と出ます。
   Claude はいつもどおり作業します（画面で様子を見たり、権限確認に答えたりできます）。
5. ターンが終わると回答が音声窓口に戻り、1〜2文の要約が読み上げられます。全文は画面に残ります。
6. 終わったら `/voice off`。

## 構成

```
 あなた（マイク / スピーカー）
   |  WebRTC 音声（エコー除去はブラウザ）
   v
 ブラウザのページ（helper/voice_page.html、127.0.0.1 で配信）
   |  SDP offer/answer をローカル HTTP で中継            音声はページ <-> OpenAI で直接流れる
   v
 helper/voice_helper.py  --stdio JSON-RPC-->  codex app-server  --(ChatGPT ログイン)-->  OpenAI realtime
   |  stdout の JSON 行（"request" など）              （同じ通話を sideband で制御）
   |  <- POST /answer {id, answer, status}
   v
 voice プラグイン（hooks/register.ts、Claude Code の中）
   |  キュー（1件ずつ）-> $.prompt.submit("[voice] ...")
   |  turn.start / turn.complete で対応づけ -> 回答を返送
   |  tool.call で音声由来ガード（hooks/guard.ts）
   v
 Claude Code のセッションが作業する
```

- 雑談は音声モデルが自分で答え、パソコンの作業が要るものは委譲（delegation）します。委譲は `handoff_request`
  として届きます。codex には何もしないよう指示し、そのターンもすぐ中断するので、**作業するのは Claude Code だけ** です。
- codex は `read-only` サンドボックス、`approval_policy="never"`、Web 検索なし、apps・plugins・computer use・
  browser use などの機能も無効で起動します。codex からの承認要求はすべて拒否します。
- **MCP の隔離（実際に保証していること）:** codex が知っている MCP サーバーを名前ごとに2段階で無効にします。
  `config.toml`（`%CODEX_HOME%` か `~/.codex`、読むだけで変更しない）にあるものは1つの
  `-c mcp_servers={"<名前>"={enabled=false}, ...}` で、さらに実効設定（`config/read`、全レイヤー）にあるものすべてを
  thread/start の上書きで。`corp.files` や `service@home` のように点や記号を含む名前も対象です。そのうえで
  `config/read` と MCP 状態一覧（`mcpServerStatus/list`）の両方が成功し、一覧のすべてが `disabled` であることを
  **必須** とし、満たさなければ起動を拒否します（`mcp_blocked` 行・トースト・ステータス行に理由が出ます）。後から
  MCP サーバーの起動が報告された場合も、realtime セッションを止めて新しい開始を拒否し、ページに赤い
  「Voice is blocked」表示を出します。codex の報告に基づく確認であり、codex を囲むサンドボックスではありません。
- ローカル HTTP サーバーは自分の `127.0.0.1:<port>` の `Host`/`Origin` しか受け付けません。ページは、ヘルパーが
  ブラウザを開くときに使う **1回限りのリンク**（`/?k=...`）でしか開けず、ページはアドレスバーからそれを消します
  （開き直しは `/voice page`）。それ以外の呼び出しには実行ごとのランダムなトークン（`X-Voice-Token`）が必要で、
  プラグインはヘルパーの `ready` 行から、ページは配信時に埋め込まれたものを使います。CORS ヘッダーは出しません。
  回答は、ヘルパー自身が現在の realtime セッションで出した依頼に対するものだけ受け付けます。本文のサイズと型の
  検査、読み取りのタイムアウト、同時処理数の上限（8）もあります。
- **対象外:** 同じ OS ユーザーで動くほかのプログラム。プロセスのメモリやコマンドラインからトークンやリンクを
  読めるため、この設計では防げません。
- 詳しくは [docs/how-it-works.md](docs/how-it-works.md)（英語）。

## 必要なもの

- **OS:** Windows 10 / 11（テスト済み）・macOS（**実験的**: 実装はしたが **実機の Mac では未テスト**。報告歓迎）。
  それ以外の OS は対象外です。
- Claude Code **2.1.289 以上**（デスクトップアプリの Code タブ、または CLI）で Mod が使えること。
  Claude Code 2.1.289（ライブ音声テスト）と 2.1.293 で確認し、0.3.0〜0.4.0 は 2.1.295 の
  `claude plugin validate/test` で確認しています。
- 音声ページを開くブラウザ:
  - Windows: Microsoft Edge（音声ページは Edge のアプリウインドウで開きます）。
  - macOS: Google Chrome・Microsoft Edge・Brave・Chromium のどれか（`/Applications` か `~/Applications`）が
    あればアプリ風のウインドウで開きます。なければ既定のブラウザで開きます（Safari でも WebRTC の音声は使えますが、
    アプリウインドウにはなりません）。
- [uv](https://docs.astral.sh/uv/) が `PATH` にあること（ヘルパーを起動します。必要なら Python 3.11〜3.13 を
  自動で用意します）。ヘルパーは標準ライブラリだけで動きます。
- [codex CLI](https://github.com/openai/codex) が `PATH` にあり、**ChatGPT でログイン済み**
  （`codex login`、確認は `codex login status`）。codex-cli 0.160.0 で確認。
  **ChatGPT のプラン（2026-10-12、codex-cli 0.160.0 で確認）:**

  | プラン | 結果 |
  | --- | --- |
  | Pro | 動きます |
  | Free | **動きません**（音声会話の接続先が `404 Not Found` を返します） |
  | Go・Plus など | まだ試していません。動くかどうか分かりません。[プランの確認方法](#chatgpt-のプランで使えるか確認する)で試して、結果を教えてください。 |
- ヘッドセットを強く推奨（スピーカーの音をマイクが拾うと割り込みが起きます）。

**macOS の注意（実験的）**

- `uv` と `codex` は Homebrew（`brew install uv`、`brew install --cask codex`）か公式のインストーラー
  （uv: `curl -LsSf https://astral.sh/uv/install.sh | sh`、codex: `npm install -g @openai/codex`）で入れます。
  Claude Code 自身から見える `PATH` に入っている必要があります。Dock からデスクトップアプリを起動して
  `/voice on` がすぐ終了する場合は、ターミナルから `claude` で起動してください。
- マイクの許可: 最初に **Start** を押すとブラウザがマイクの許可を求め、macOS も確認することがあります。
  拒否してしまった場合は **システム設定 → プライバシーとセキュリティ → マイク** でそのブラウザ（Chrome・Edge・
  Brave・Chromium・Safari）を許可し、ブラウザを再起動してください。
- データフォルダ: `~/Library/Application Support/claude-code-voice-mod/`（「設定」を参照）。
- codex は専用のプロセスグループで動き、ヘルパーの終了時にグループごと終了させます。ヘルパー自身が強制終了された
  場合は、小さな見張りプロセスがグループを終了させます。これは Windows 上でモックを使ったユニットテストでしか
  確かめていません。

## インストール

### Claude Code にインストールしてもらう（いちばん簡単）

Claude Code を開き（どのフォルダでも可）、たとえば次のように貼り付けます:

> https://github.com/ldk00315-jpg/claude-code-voice-mod の README.ja.md を読んで、私の PC にインストールして。
> 設定や環境変数を変える前には必ず確認して

Claude Code が下の手順に沿って進めます。codex へのログイン、Claude Code が実行したいコマンドの承認、最後の
新しいセッションの開始（または `/reload-plugins`）は、あなた自身が行います。

### インストール手順（人と Claude Code 向け）

> [!IMPORTANT]
> **この手順に沿って作業する AI エージェントへのルール**
>
> - **認証情報は扱わない。** ユーザーの代わりに `codex login` を実行しない。`~/.codex/auth.json` やトークン・
>   API キー・パスワードを読む・コピーする・表示する・編集することはしない。`codex login status` が ChatGPT
>   ログインを示さなければ、そこで止まり、ユーザー自身に `codex login` を実行してもらう。
> - **このリポジトリの外を変える前には必ず確認する。** ソフトのインストール、マーケットプレイスの追加、プラグインの
>   インストール、環境変数（`CLAUDE_CODE_PLUGIN_DIRS`、`VOICE_MOD_DATA_DIR`）の設定、`~/.claude/settings.json`
>   などの設定ファイルの編集。実行するコマンドをそのまま見せ、了承を待つ。
> - **自動承認を広げない。** `permissions.allow` のルール追加、権限確認を省くモードへの切り替え、
>   `--dangerously-skip-permissions` の使用はしない。音声ガードは Claude Code の通常の権限確認に上乗せする
>   補助にすぎない。
> - ユーザーの代わりに `/voice on` を実行したり、マイクを開いたりしない。やり方を伝える（手順 5）。
> - 最後に、確認したことと変更したことを報告する。

**1. OS を確認する。** Windows 10 / 11、または macOS（実験的: 実機の Mac では未テストだとユーザーに伝える）。
それ以外なら中止。

**2. 前提条件を確認する。** コマンドは Windows の PowerShell でも macOS のターミナルでも同じです。

| 確認 | コマンド | 期待する結果 | だめなとき |
| --- | --- | --- | --- |
| Claude Code | `claude --version` | `2.1.289` 以上（手順 3 のシェル1コマンド版は `2.1.292` 以上） | Claude Code を更新。`claude` が見つからない（デスクトップアプリだけ）場合は、手順 3 のセッション内コマンドを使う。 |
| uv | `uv --version` | 版は問わない | Windows: `winget install --id=astral-sh.uv -e`。macOS: `brew install uv`。または[公式インストーラー](https://docs.astral.sh/uv/getting-started/installation/)。先に確認すること。 |
| codex | `codex --version` | `codex-cli ...`（0.160.0 で確認） | `npm install -g @openai/codex`、macOS なら `brew install --cask codex` も可。先に確認すること。 |
| codex のログイン | `codex login status` | **ChatGPT で**ログイン済み（API キーではない） | **ユーザー本人**が `codex login` を実行し ChatGPT でサインイン。エージェントは行わない。 |
| ブラウザ | - | Windows: Edge（標準で入っている）。macOS: Chrome・Edge・Brave・Chromium を推奨、なければ既定のブラウザ | 任意: どれかを入れる。 |

`uv` や `codex` を入れた後は、`PATH` を反映させるため新しいターミナルを開いてから確認し直します。

**3. プラグインをインストールする（おすすめ: GitHub のマーケットプレイスから）。** ユーザースコープ（この PC の
全プロジェクト）に入り、`~/.claude/settings.json` に記録されるので、先に確認します。

シェルから（ユーザーの了承後ならエージェントが実行してよい）:

```
claude plugin marketplace add ldk00315-jpg/claude-code-voice-mod
claude plugin install voice@claude-code-voice-mod
```

Claude Code 2.1.292 以上なら1コマンドでも可:

```
claude plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

または Claude Code のセッション内で（ユーザーが入力。Claude Code 2.1.275 以上）:

```
/plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

マーケットプレイス追加の確認に `y`、スコープを選びます。セッション内でも2段階の形が使えます:
`/plugin marketplace add ldk00315-jpg/claude-code-voice-mod` のあと `/plugin install voice@claude-code-voice-mod`。
（`voice` はプラグイン名、`claude-code-voice-mod` は `.claude-plugin/marketplace.json` のマーケットプレイス名。
書式の出典: Claude Code ドキュメント [Install and manage plugins](https://code.claude.com/docs/en/discover-plugins)。）

マーケットプレイスを使わない方法（開発用、手元のコピーを試すとき）: リポジトリを好きなフォルダ（例:
`C:\tools\claude-code-voice-mod`、`~/tools/claude-code-voice-mod`）に clone かダウンロードし、

- CLI で1セッションだけ: `claude --plugin-dir <フォルダ>`、または
- （先に確認）環境変数 `CLAUDE_CODE_PLUGIN_DIRS=<フォルダ>` を設定（ユーザー環境変数、または
  `~/.claude/settings.json` の `env`。複数のフォルダは Windows では `;`、macOS では `:` で区切る）して
  Claude Code を再起動。

ヘルパーはプラグインの中の `helper/` にあり、プラグインのフォルダ（`$.plugin.root`）からの相対位置で見つけるので、
パスの設定は要りません。ヘルパーのデータはプラグインのフォルダに置かないので、インストールされたコピーでも動きます。

**4. 読み込む。** シェルから入れたプラグインは次の Claude Code セッションで読み込まれます。開いているセッションでは
`/reload-plugins` を実行します。エージェントは自分のセッションを再起動できないので、ユーザーに新しいセッションを
始めてもらいます（デスクトップアプリは新しいセッション。環境変数を変えた場合はアプリを再起動）。
`claude plugin list`（`voice@claude-code-voice-mod` が出る）と、セッション内の `/voice status`
（`voice: stopped` が返る）で確認します。

**5. 初回の動作確認（ユーザーが行う）。** Claude Code のセッションで `/voice on` と入力。音声ページが開くので
**Start (uses the microphone)** を押し、マイクを許可します（macOS はシステム設定 → プライバシーとセキュリティ →
マイクでそのブラウザも許可）。「こんにちは」と話して返事があれば成功です。終わったら `/voice off`。うまくいかない
ときは[トラブルシューティング](#トラブルシューティング)へ。

## 使い方

| コマンド | 内容 |
| --- | --- |
| `/voice on [名前]` | ヘルパーを起動し、音声ページを開く。`名前` はスレッド名（省略可）。 |
| `/voice off` | ヘルパー（と codex）を止める。セッション終了時にも止まる。 |
| `/voice page` | 音声ページを開き直す（新しい1回限りのリンク）。ウインドウを閉じてしまったときなど。 |
| `/voice status` | 状態、ヘルパーのポート、待ち・実行中の依頼、ガードが有効か。 |

音声ページでは:

- **Start (uses the microphone)** で会話開始、**Stop** で終了、**Mute** でマイクをミュート。
- **Type to talk** は文字を会話に送ります。ただし文字は「文脈」として追加され、テストでは **委譲が起きません** でした。
  作業の依頼は声でしてください。
- 依頼は **1件ずつ** 処理し、後の依頼は順番待ちになります。10分を超えた依頼は「timeout」として音声で伝え、
  その後 Claude が終われば遅れて結果を読み上げます。
- Claude の権限確認は声では答えられません。画面で承認してください。

### 声の選択と試聴

**Voice** の一覧は codex（`thread/realtime/listVoices`、失敗時は固定の一覧）から取ります。選んだ声は
`settings.json`（下の「設定」）に保存され、次回も使われます。会話中は声を変えられないので、一覧はロックされます。
**Preview** は選んだ声で1文の自己紹介をしてもらう機能で、マイクは使わず（無音のトラックを送ります）、自動で止まります。

### スレッド名

ウインドウがどの Claude Code のスレッドにつながっているかを示す名前で、複数の音声ウインドウを見分けるのに使います。
ウインドウのタイトルと見出しに出て、音声モデルにも伝えます（「どのスレッド？」と聞けば答えます）。`/voice on <名前>`
で付けるか、ページで変更します。名前は Claude Code のセッションごとにプラグインの `$.store` に保存され、
`/voice off`、再起動、再開をまたいで残ります。名前が無いときは `<フォルダ名>・<セッション ID の先頭4文字>` です。
ページで変えた名前が音声モデルに伝わるのは、次に **Start** したときからです。

## 設定: キャラクター（persona）と声

ヘルパーのデータは **プラグインのフォルダの外**、ユーザーごとのフォルダに置きます:

- 既定は Windows が `%LOCALAPPDATA%\claude-code-voice-mod\`、macOS が
  `~/Library/Application Support/claude-code-voice-mod/`。環境変数 `VOICE_MOD_DATA_DIR`（またはヘルパーの
  `--data-dir`）で変更できます。
- `settings.json`（声と persona）、`logs\voice-*.log`、`logs\voice-answers.jsonl`（メタ情報のみ: id・状態・長さ・
  短いハッシュ。回答本文なし）、codex の作業フォルダにする空の `codex-cwd\`。
- **会話のログは既定でオフです。** ログに残すのはメタ情報だけ（状態・エラー・id・サーバー名・各テキストの長さ）で、
  話した内容・依頼・回答は残しません。デバッグのときは、ヘルパーを `--debug-log` で起動するか `settings.json` に
  `"debugLog": true` を書くと、文字起こしと依頼文も（秘密情報らしい文字列を伏せ字にして、ベストエフォート）残ります。
  回答本文はどちらでも記録しません。
- 保持期間: ヘルパーの起動のたびに、7日より古いログファイルを消し、さらに合計 20 MB に収まるまで古い順に消します。
  `voice-answers.jsonl` は 1 MB を超えたら切り替えます。全部消すときは、ヘルパーを止めてから `logs` フォルダを削除してください。

既定では、**話しかけた言語（日本語や英語など）で答える、ニュートラルで親しみやすい音声アシスタント** です。
キャラクターを付けたいときは `settings.json` に `persona` を書きます
（[helper/settings.example.json](helper/settings.example.json) をコピー）:

```json
{
  "voice": "maple",
  "persona": {
    "name": "Mika",
    "userName": "Alex",
    "language": "Japanese",
    "style": "bright, casual and warm, like a friendly classmate; short sentences, a little playful",
    "fillers": ["見てくるから少し待っててね", "OK, let me look into that"],
    "instructions": "Call the user's computer 'the workshop'. Never use more than two sentences.",
    "previewText": "この声で、ひとことだけ自己紹介して。"
  }
}
```

| 項目 | 既定 | 意味 |
| --- | --- | --- |
| `name` | なし | アシスタントの名前。 |
| `userName` | なし（"the user"） | あなたの呼び方。 |
| `language` | 話しかけた言語 | 言語を固定する（例: `"Japanese"`）。 |
| `style` | 親しみやすく落ち着いて短く | 話し方（自由記述。方言やキャラクターの口調はここに書く）。 |
| `fillers` | "Let me check that." | 作業を Claude に渡した直後に言う短いひとこと（最大6個）。 |
| `instructions` | なし | 音声モデルへの指示に追加する自由記述。 |
| `previewText` | 1文の自己紹介 | 試聴ボタンで話してもらう内容。 |

どれも省略できます。persona はヘルパー起動時に読み込むので、変えたら `/voice off` → `/voice on` で反映されます。
persona が影響するのは音声モデルだけで、Claude Code にはニュートラルな `[voice] ...` の依頼文が届き、
依頼の言語で答えます。

## 音声由来ガード

音声から来たターンの実行中と、その中で始まったすべてのサブエージェント（ターン終了後も動くバックグラウンドの
エージェントを含む）で、`tool.call` が次のものを **拒否** します:

| 種類 | 例 |
| --- | --- |
| 再帰的・破壊的な削除 | `rm -r`/`-rf`/`--recursive`、`del /s`、`rmdir /s`、`rd /s`、`Remove-Item -Recurse`（省略形も）、`format X:`、`Format-Volume`、`Clear-Disk`、`diskpart`、`mkfs`、`git clean -f`、`git reset --hard` |
| push | `git push`（`git -C x push` も） |
| GitHub CLI の書き込み | `gh pr create/merge/close/edit/comment/review/...`、`gh issue create/close/...`、`gh release`、`gh repo edit/delete/create/...`、gist/secret/variable/workflow の書き込み、書き込みメソッドやフィールド付きの `gh api` |
| パッケージの公開 | `npm/pnpm/yarn/bun publish`、`uv/poetry/flit/hatch/pdm publish`、`twine upload`、`cargo publish`、`gem push`、`nuget push`、`vsce publish`、`docker push` |
| 送信 | `Send-MailMessage`、`sendmail`、`mail -s`、POST/PUT/PATCH/DELETE やデータ・フォーム・アップロード付きの `curl`、`Invoke-WebRequest/RestMethod -Method Post...`、`wget --post-*` |
| 公開 | `Artifact` の publish/delete/pin など（read/list/open/quickstart 以外）、`ArtifactData` の書き込み、`ArtifactComments` の投稿 |
| ブラウザ・画面の操作 | `mcp__computer-use__*`、`mcp__Claude_Browser__*`、`mcp__claude-in-chrome__*`、`mcp__Control_Chrome__*` |
| 予約・定期実行 | `schtasks /create /change /delete /run`、`*-ScheduledTask`、`crontab`、`CronCreate/Delete/Update`、`RemoteTrigger`、`ScheduleWakeup`、予約タスク系 MCP ツール（list/get 以外）、`schedule`/`loop` スキル |
| 外部に出る MCP ツール | 名前に `send` `post` `publish` `delete` `trash` `share` `create_event` を含む MCP ツール、`set_setting`、`set_session_permission_mode` |

拒否するとトースト（`voice: blocked ...`）が出て、Claude には「最終回答でそのことを伝えて」と返すので、
音声窓口が「キーボードから頼み直してね」と伝えられます。**手入力のターンは一切チェックしません。**

限界:

- コマンド文字列の照合で、`|`・`;`・`&&` で区切った部分ごとに判定します。スクリプトの中身は見ません
  （ファイルを消す `python cleanup.py` は止められません）。逆に危険な語を含むだけの文字列（`echo "rm -rf"`）も止めます（安全側）。
- ファイルの読み取りや編集は許可されます。聞き間違いの依頼でもファイルが変わりえます。
- プロンプトに音声マーカー（`[voice] ` で始まる行と、プラグインの注意書きブロック）があるか、プラグインが送った
  テキストであれば、その依頼がその後どうなっても（タイムアウト、`/voice off`）音声由来のターンとして扱います。
- **音声マーカー全体を含む手入力のプロンプト**（音声のプロンプトを貼り付けた場合など）もガード対象です（安全側の
  設計）。ガードなしで実行したいときは、マーカーを外して打ち直してください。
- ガードの状態（実行中のターン、どのサブエージェントが音声ターンのものか、送ったテキスト）は変わるたびに
  セッションの `$.state` に保存するので、音声ターンの途中でプラグインがホットリロードされてもガードは続きます。
  リロード後にその状態を読めなかった場合は、実行中のターンとその間に初めて見たサブエージェントを、次のターンの
  開始か終了まで音声ターンとして扱います（安全側）。保存に失敗したときはステータス行に `guard NOT saved (retrying)`
  （トーストも1回）が出ます。動いているモジュールではガードが続き、以後のフックのたびに保存を再試行します。
  リロードしたモジュールは会話履歴も読み、実行中のターンのプロンプトに音声マーカーがあれば、保存状態が無い・古い
  場合でもそのターンをガードします。
- **保存に失敗している間の既知の制限。** 状態を保存できず、**さらに**プラグインが読み込み直された場合、次の2つでは
  ガードが外れることがあります。(a) 会話履歴も読めず、実行中の音声ターンだと分からない場合。(b) 音声ターンが
  始めたバックグラウンドのサブエージェントが、そのターンの終了後も動き続けている場合（履歴にはもう実行中の音声
  ターンがなく、そのサブエージェントの由来も保存されていない）。そのため、**`guard NOT saved` が出ている間は、
  プラグインの読み込み直しや更新をせず、バックグラウンドの作業も動かしたままにしないでください。** 警告が消える
  のを待つか、`/voice off` で止めて、動いている作業は Claude の通常の権限確認のもとで終わらせてください。
- 音声ターンのサブエージェントは、状態を保存できている限りセッション中ずっと覚えます（消しません。上の制限を参照）。
  手入力ターンのサブエージェントは 500 件まで覚え、忘れたものが音声ターン中に現れたら音声由来として扱います。
- 補助的な安全網であって、安全境界ではありません。セキュリティ対策として頼らないでください。

## トラブルシューティング

| 症状 | 確認すること |
| --- | --- |
| `/voice on` 直後にヘルパーが終了する | `uv` と `codex` が `PATH` にあるか。`uv run --script <プラグインのフォルダ>\helper\voice_helper.py --help` を試す。macOS はターミナルから Claude Code を起動して Homebrew の `PATH` が見えるようにする。 |
| `codex not found on PATH` | codex CLI を入れる。`where codex`（macOS は `which codex`）で確認。 |
| `realtime state=error` / 401 / `unauthorized` | `codex login status` が ChatGPT ログインになっているか。ログインし直す。 |
| `realtime conversation requires API key auth` | WebRTC で接続できていない。codex の版で API が変わった可能性。 |
| `AVAS realtime calls require realtime v1 or v3` | ChatGPT ログインで使えるのは v1/v3 だけ（既定は v3）。 |
| macOS: ウインドウが開かない | Chrome・Edge・Brave・Chromium を入れるか、既定のブラウザが `http` のリンクを開けるか確認し、`/voice page`。 |
| macOS: マイクが無音・拒否される | システム設定 → プライバシーとセキュリティ → マイクでブラウザを許可し、ブラウザを再起動して `/voice page`。 |
| Start で "failed" | ブラウザ（Windows は Edge）のマイク許可、既定の録音デバイス、他のアプリがマイクを専有していないか。ページは `127.0.0.1` で開くこと。 |
| `media-failed` / `media lost` | ファイアウォールや VPN が UDP を止めている可能性。 |
| 音が出ない | ブラウザのタブがミュートでないか、出力デバイス。 |
| 自分の声で割り込まれる | ヘッドセットを使う（エコー）。 |
| 委譲せずに自分で答えてしまう | もっと具体的に頼む（「…のファイルを見て」）。`persona.instructions` で調整。 |
| 回答が読み上げられない | ステータス行に理由（`answer not spoken: ...`）が出ます。ページのログの `Result → speech` も確認。 |
| `voice: blocked: ...`、ページに「Voice is blocked」 | MCP の隔離を確認できませんでした（上の説明を参照）。ログで理由を確認し、そこに出た MCP サーバーを外すか直してから `/voice off` → `/voice on`。 |
| ページに「this link is used up」 | ページは1つのリンクで1回しか開けません。`/voice page` を実行。 |
| その他 | `%LOCALAPPDATA%\claude-code-voice-mod\logs\voice-*.log`（macOS は `~/Library/Application Support/claude-code-voice-mod/logs/`）（既定ではメタ情報だけ。デバッグログをオンにした場合は文字起こしと依頼文が残るので、共有前に確認してください）。 |

## 開発

```
claude plugin validate .
claude plugin test .
uv run --project helper python -m unittest discover -s helper/tests
```

実サービスにつなぐヘッドレスの自己テスト（ChatGPT プランを使います。マイクは不要）:
`uv run --project helper --group selftest python helper/voice_helper.py --selftest-text "Hello"` と
`--selftest-handoff "..."`（ヘルパーの docstring を参照）。スクリプトの前の `python` を忘れないでください。
ヘルパーにはスクリプト用のメタデータがあるため、`uv run ... helper/voice_helper.py` の形だと `--group selftest` が
無視され、`No module named 'aiortc'` で失敗します。

### ChatGPT のプランで使えるか確認する

インストールの前や、音声がつながらないときに、自己テストを1回実行してください（マイクは使いません。
「Hello」という文字を OpenAI に送るので、プランの利用枠を少しだけ使います）。リポジトリのフォルダで:

```
uv run --project helper --group selftest python helper/voice_helper.py --selftest-text "Hello"
```

初回はテスト用の部品（aiortc など）をダウンロードするので、1〜2分かかることがあります。

| 出力 | 意味 |
| --- | --- |
| `"type": "transcript", "role": "assistant"` の行と `"assistantTranscript": true` | このプランで使えます。 |
| `unexpected status 404 Not Found ... realtime/calls` | このプランでは音声会話を使えません（Free プランはこの結果でした）。 |
| `401` / `unauthorized` | ログインしていません。ご自身で `codex login` を実行してから、もう一度試してください。 |

ふだんのログインを変えずに**別の ChatGPT アカウント**で試すには、PowerShell のウインドウを1つ開き、
次をすべてそのウインドウの中で行います:

1. `$env:CODEX_HOME = "$env:TEMP\codex-plan-test"; New-Item -ItemType Directory -Force $env:CODEX_HOME | Out-Null; codex login`
2. 自動で開いたブラウザのタブは閉じます（ふだんのアカウントのままのため）。ウインドウに表示された
   `https://auth.openai.com/...` の URL をコピーし、シークレットウインドウに貼り付けて、試したいアカウントで
   ログインします。
3. `codex login status` で `Logged in using ChatGPT` と出ることを確認します。
4. 上の自己テストを実行します。
5. ウインドウを閉じ、`%TEMP%\codex-plan-test` を削除します（そのアカウントのログイン情報が入っています）。

ブラウザに「State mismatch」と出たら、前の `codex login` がまだ待っています。そのウインドウで Ctrl+C して止め、
1 からやり直してください。

## セキュリティレビュー

第三者によるセキュリティレビューを4回受けました（静的なレビューとモックを使った検証。実 API の呼び出し、
マイクの録音、侵入テストは含みません）。各回の指摘は次の版で直しています（[CHANGELOG.md](CHANGELOG.md)）。

| 回 | 対象コミット | 結果 |
| --- | --- | --- |
| 1 | `2dbfc29`（0.2.0） | タイムアウト・`/voice off` 後のガードのすり抜け、認証なしのローカル HTTP、MCP の漏れ、回答の伏せ字なし。0.2.1 で修正。 |
| 2 | `553e449`（0.2.1） | ターン途中のホットリロード、ページからのトークン取得、ドットを含む MCP 名、会話ログが既定で有効、HTTP の上限。0.3.0 で修正。 |
| 3 | `d3a47fa`（0.3.0） | 保存できなかったガード状態がリロードで消える、古い回答が新しいセッションに再送される、500 件を超えたエージェントの削除。0.3.1 で修正。 |
| 4 | `deeac60`（0.3.1、タグ `v0.3.1`） | これまでの修正を確認。残りは[音声由来ガード](#音声由来ガード)に書いた「保存に失敗している間の既知の制限」の2つ。実在する秘密情報や、認証なしで外部からコードを実行される問題は見つかっていません。 |

**0.4.0 全体はこのレビューの対象外です。** macOS 向けのコード（ブラウザの起動、プロセスグループと見張り
プロセス、データフォルダ、POSIX のシグナル）と README のインストール手順は、レビュー済みの `v0.3.1` の後に
追加したもので、**セキュリティレビューも実機の Mac でのテストも受けていません**。Windows 上でモックを使った
ユニットテストで確かめているだけです。Windows 向けの動作は変えていません。

0.3.1 に対するレビューの判断: 自分での利用と、OpenAI への送信・Claude と同じ権限・実験的 API を理解した人による
少人数での試用には使ってよい。危険な操作を確実に防ぐガードとして配布するのは不可。本物の Claude Code での
ホットリロード、Codex/OpenAI との実際のセッション、別の OS ユーザーからのアクセスは、レビューでは試していません。

## ライセンス

MIT（[LICENSE](LICENSE)）。
