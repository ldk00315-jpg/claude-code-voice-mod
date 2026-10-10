# claude-code-voice-mod

[English README](README.md)

Windows で、Claude Code のセッションに声で話しかけるための Mod です。

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
>   秘密鍵、`password = ...` のような行、接続文字列の認証情報、長いランダム文字列）を `[REDACTED]` に置き換えますが、
>   **ベストエフォート** です（よくある形式しか分かりません）。
> - **誤作動しえます。** 周りの話し声やテレビ、聞き間違いが Claude への依頼になることがあります。
>   音声の依頼は **Claude Code のセッションと同じ権限** で実行されます。
> - **ガードはサンドボックスではありません。** 音声由来のターンの危険な操作は文字列ベースのガード（後述）で
>   止めますが、簡単にすり抜けられます（例: ファイルを消すスクリプトを実行する）。既知のパターンしか止めません。
> - **秘密情報・認証情報のあるフォルダでは使わないでください。** 読み上げられたり OpenAI に送られたりして困るものが
>   ある場所では `/voice on` しないこと。ヘッドセットを使い、使わないときは `/voice off` してください。
>
> 本ソフトウェアは無保証で「現状のまま」提供されます（[LICENSE](LICENSE)）。

## デモの流れ

1. Claude Code で `/voice on docs-site` と入力。Edge のアプリウインドウ（「[docs-site] Voice Mod」）が開きます。
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
 Edge のページ（helper/voice_page.html、127.0.0.1 で配信）
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
- codex は `read-only` サンドボックス、`approval_policy="never"`、Web 検索なし、MCP サーバーすべて無効、
  apps・plugins・computer use・browser use などの機能も無効で起動します。MCP サーバーは名前ごとに2段階で止めます:
  `config.toml`（`%CODEX_HOME%` か `~/.codex`、読むだけで変更しない）にあるものは `-c mcp_servers.<名前>.enabled=false`、
  それでも実効設定（`config/read`、全レイヤー）で有効なものはスレッド単位の上書きで無効化します（codex は `-c` の
  テーブルをマージするので、1つの上書きで一覧を空にはできません）。それでも起動した MCP サーバーがあれば、ヘルパーは
  WARN をログに書き、`mcp_warning` 行を出します。codex からの承認要求はすべて拒否します。
- ローカル HTTP サーバーは自分の `127.0.0.1:<port>` の `Host`/`Origin` しか受け付けず、ページの読み込み以外の
  すべての呼び出しに実行ごとのランダムなトークン（`X-Voice-Token`）が必要です。トークンはプラグイン向けに
  ヘルパーの `ready` 行に、ページ向けには配信するページに埋め込まれます。CORS ヘッダーは出さないので、
  ほかの Web ページからは読めません。
- 詳しくは [docs/how-it-works.md](docs/how-it-works.md)（英語）。

## 必要なもの

- Windows 10 / 11（ヘルパーは Edge を開き、Windows のジョブオブジェクトで codex を道連れ終了させます）。
- Claude Code **2.1.289 以上**（デスクトップアプリの Code タブ、または CLI）で Mod が使えること。
  Claude Code 2.1.289（ライブ音声テスト）と 2.1.293（`claude plugin validate/test`）で確認しています。
- Microsoft Edge（音声ページは Edge のアプリウインドウで開きます）。
- [uv](https://docs.astral.sh/uv/) が `PATH` にあること（ヘルパーを起動します。必要なら Python 3.11〜3.13 を
  自動で用意します）。ヘルパーは標準ライブラリだけで動きます。
- [codex CLI](https://github.com/openai/codex) が `PATH` にあり、**ChatGPT でログイン済み**
  （`codex login`、確認は `codex login status`）。codex-cli 0.160.0 で確認。
  動作を確認したのは ChatGPT の **Pro** プランだけです。Free・Go・Plus などほかのプランで
  音声会話が使えるかは未確認です。試した方はぜひ教えてください。
- ヘッドセットを強く推奨（スピーカーの音をマイクが拾うと割り込みが起きます）。

## インストール

このリポジトリを好きなフォルダ（例: `C:\tools\claude-code-voice-mod`）に clone かダウンロードします。
リポジトリのルートがそのままプラグインで、ヘルパーは中の `helper/` にあります。プラグインは自分のフォルダ
（`$.plugin.root`）からの相対位置でヘルパーを見つけるので、パスの設定は要りません。

**方法 A: フォルダから読み込む（試すときのおすすめ）**

- CLI で1セッションだけ: `claude --plugin-dir C:\tools\claude-code-voice-mod`
- デスクトップアプリや全セッション: 環境変数 `CLAUDE_CODE_PLUGIN_DIRS=C:\tools\claude-code-voice-mod` を設定
  （ユーザー環境変数、または `~/.claude/settings.json` の `env`）して Claude Code を再起動。

**方法 B: マーケットプレイスからインストール**（GitHub に公開した後）:

```
/plugin install voice --marketplace ldk00315-jpg/claude-code-voice-mod
```

マーケットプレイス追加の確認に `y`、スコープを選びます。ヘルパーのデータはプラグインのフォルダに置かないので、
インストールされたコピーでもそのまま動きます。

確認: `/voice status` が `voice: stopped` を返せば読み込めています。

## 使い方

| コマンド | 内容 |
| --- | --- |
| `/voice on [名前]` | ヘルパーを起動し、音声ページを開く。`名前` はスレッド名（省略可）。 |
| `/voice off` | ヘルパー（と codex）を止める。セッション終了時にも止まる。 |
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

- 既定は `%LOCALAPPDATA%\claude-code-voice-mod\`。環境変数 `VOICE_MOD_DATA_DIR` で変更できます。
- `settings.json`（声と persona）、`logs\voice-*.log`、`logs\voice-answers.jsonl`（メタ情報のみ: id・状態・長さ・
  短いハッシュ。回答本文なし）、codex の作業フォルダにする空の `codex-cwd\`。
- ログには回答本文を書きません（id・長さ・短いハッシュだけ）。文字起こしと依頼文はデバッグ用に残しますが、
  秘密情報らしい文字列は伏せ字にします（ベストエフォート）。

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
  マーカー全体を含む手入力のプロンプト（音声のプロンプトを貼り付けた場合など）もガード対象です（安全側の設計）。
- ガードの状態はモジュール内にあるので、音声ターンの途中でプラグインがホットリロードされると、そのターンの追跡が
  切れます（新しい音声ターンは正常に追跡されます）。
- シートベルトであって、サンドボックスではありません。セキュリティ対策として頼らないでください。

## トラブルシューティング

| 症状 | 確認すること |
| --- | --- |
| `/voice on` 直後にヘルパーが終了する | `uv` と `codex` が `PATH` にあるか。`uv run --script <プラグインのフォルダ>\helper\voice_helper.py --help` を試す。 |
| `codex not found on PATH` | codex CLI を入れる。`where codex` で確認。 |
| `realtime state=error` / 401 / `unauthorized` | `codex login status` が ChatGPT ログインになっているか。ログインし直す。 |
| `realtime conversation requires API key auth` | WebRTC で接続できていない。codex の版で API が変わった可能性。 |
| `AVAS realtime calls require realtime v1 or v3` | ChatGPT ログインで使えるのは v1/v3 だけ（既定は v3）。 |
| Start で "failed" | Edge のマイク許可、既定の録音デバイス、他のアプリがマイクを専有していないか。ページは `127.0.0.1` で開くこと。 |
| `media-failed` / `media lost` | ファイアウォールや VPN が UDP を止めている可能性。 |
| 音が出ない | Edge のタブがミュートでないか、出力デバイス。 |
| 自分の声で割り込まれる | ヘッドセットを使う（エコー）。 |
| 委譲せずに自分で答えてしまう | もっと具体的に頼む（「…のファイルを見て」）。`persona.instructions` で調整。 |
| 回答が読み上げられない | ステータス行に理由（`answer not spoken: ...`）が出ます。ページのログの `Result → speech` も確認。 |
| その他 | `%LOCALAPPDATA%\claude-code-voice-mod\logs\voice-*.log`（SDP の認証情報や秘密情報らしい文字列は伏せ字、回答本文は記録しませんが、文字起こしと依頼文は残ります。共有前に確認してください）。 |

## 開発

```
claude plugin validate .
claude plugin test .
uv run --project helper python -m unittest discover -s helper/tests
```

実サービスにつなぐヘッドレスの自己テスト（ChatGPT プランを使います。マイクは不要）:
`uv run --project helper --group selftest helper/voice_helper.py --selftest-text "Hello"` と
`--selftest-handoff "..."`（ヘルパーの docstring を参照）。

## ライセンス

MIT（[LICENSE](LICENSE)）。
