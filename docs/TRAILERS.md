# Models from git trailers, and the opt-in done rules

English first, Korean below.

## Which model worked where

DropViz reads the commit messages of the default branch. A line counts when it is one of

- `Co-Authored-By:` / `Co-Developed-By:` naming an AI tool or model (a person's name is never counted, and `Claude Monet` is a person);
- `Assisted-By:` / `Generated-By:` (written `agent:model` or just a name);
- a `Generated with ...` line, only when the commit names no model in a trailer.

Only the **name** is kept. E-mail addresses in a trailer are dropped and never shown on the page, in the API or in the files DropViz writes.

Names go through a small table (`trailer.models` in `src/dropviz/defaults.json`). A name the table does not know keeps its own words.

| Written in the trailer | Shown as |
|---|---|
| `Claude Opus 5.5`, `claude-opus-5-5-20260101`, `Claude Opus 5.5 (1M context)` | `Claude Opus 5.5` |
| `Claude` | `Claude` (the model is not named) |
| `Generated with [Claude Code]` | `Claude Code` |
| `GPT-5.1 Codex`, `Assisted-by: Codex:gpt-5.1-codex` | `GPT-5.1 Codex` |
| `Gemini 2.5 Pro`, `Qwen3-Coder`, `Copilot`, `Cursor`, `Devin` ... | the same words |

Counting: a commit counts once for each model named on it, and its changed lines (added + deleted, from `git log --numstat`) are shared equally between those models, so shares add up. A commit that touches several features counts in each of them; an area counts a commit once. The first and last dates are commit dates. Only commits on the default branch are read, in one `git log` call (about 5 s for 13,000 commits). When more than 8,000 commits name a model, reading every diff takes minutes (283 s for a 128k-commit repo, against 14 s for file names only), so the count switches to changed **files** (one per file) and the legend says "changed files".

These numbers come from what people wrote into commit messages. They are not measured. **Tokens stay unknown (`?`)** unless local AI-tool records exist on your computer; the Models tab shows the two sources apart, "Models named in commit trailers" and "Measured locally".

In the **Models view** a tile is split into bands, one per model, as wide as that model's share of changed lines; the area header shows its biggest model. Tiles with no model named stay grey.

## Done rules

The default is `strict`: merged into the default branch **and** a verification record. Two opt-in rules for repos that keep no verification records:

| Rule | A feature is done when |
|---|---|
| `strict` (default) | its latest change is merged and a verification record exists at or after it |
| `merged` | its latest change is merged, no newer unmerged work touches it, and no failing check is recorded |
| `merged+ci` | as `merged`, and the checks of that commit passed |

Choose with `dropviz serve . --done merged` (also `timelapse`, `badge`, `metrics`, `init`, `configure`, `mcp`), the environment variable `DROPVIZ_DONE`, or `done: merged` in `dropviz.yaml`. The legend of the Progress view always says which rule is in use.

`merged+ci` reads checks from `.dropviz/ci-status.json` first, then from the GitHub API for the repo's `origin` (read only; a public repo needs no token; `GITHUB_TOKEN` or `GH_TOKEN` raises the rate limit). No other rule touches the network. Answers are cached per commit in `.dropviz/`, at most 40 calls per run without a token (400 with one), and reading stops when the rate limit is nearly used. A commit whose checks cannot be read is not done under `merged+ci`, and the feature's reason says why. In a replay, past days under `merged+ci` use the `merged` rule; the last frame is the real current map.

## The replay grows

A feature that no commit has touched yet is not drawn in the replay: its slot stays empty (dark, dashed) and fills in on the day of its first commit.

---

## 한국어

### 어느 모델이 어디서 일했나

기본 브랜치의 커밋 메시지를 읽습니다. 다음 줄이 센 대상입니다: AI 도구·모델 이름이 적힌 `Co-Authored-By:` / `Co-Developed-By:`(사람 이름은 세지 않음, `Claude Monet` 은 사람), `Assisted-By:` / `Generated-By:`, 그리고 꼬리말에 모델 이름이 없는 커밋의 `Generated with ...` 줄.

**이름만** 둡니다. 꼬리말의 이메일 주소는 버리며 화면·API·DropViz 가 쓰는 파일 어디에도 나오지 않습니다. 이름은 작은 표(`src/dropviz/defaults.json` 의 `trailer.models`)를 거치고, 표에 없는 이름은 적힌 그대로 둡니다.

세는 법: 한 커밋은 거기 적힌 모델마다 한 번 세고, 바뀐 줄(`git log --numstat` 의 더한 줄 + 지운 줄)은 그 모델들이 똑같이 나눠 가집니다. 여러 기능에 닿은 커밋은 기능마다 세고, 영역은 한 번만 셉니다. 처음·마지막 날짜는 커밋 날짜입니다. 기본 브랜치만, `git log` 한 번으로 읽습니다(커밋 13,000개에 약 5초). 모델 이름이 적힌 커밋이 8,000개를 넘으면 diff 를 모두 읽는 데 몇 분이 걸려(커밋 128k개짜리 저장소에서 283초, 파일 이름만 읽으면 14초) 바뀐 **파일** 수(파일당 1)로 세고 범례에 「바뀐 파일」이라고 적습니다.

이 값은 사람이 커밋 메시지에 적은 것이지 측정한 값이 아닙니다. **토큰은 모름(`?`)** 으로 둡니다(내 컴퓨터에 AI 도구 기록이 있을 때만 측정값이 나옴). 모델 탭은 두 출처를 따로 보입니다: 「커밋 꼬리말에 적힌 모델」과 「이 컴퓨터에서 측정」.

**모델 화면**에서는 타일이 모델별 띠로 나뉩니다(띠 너비 = 그 모델이 바꾼 줄의 비율). 영역 머리에는 가장 큰 모델이 보입니다. 모델 이름이 없는 타일은 회색입니다.

### 완료 규칙

기본은 `strict`: 기본 브랜치에 병합 **그리고** 검증 기록. 검증 기록을 두지 않는 저장소용으로 고르는 규칙 둘: `merged`(마지막 변경이 병합되고, 더 새로운 미병합 작업이 없고, 실패한 검사 기록이 없을 때), `merged+ci`(`merged` + 그 커밋의 검사 통과).

`dropviz serve . --done merged`, 환경변수 `DROPVIZ_DONE`, 또는 `dropviz.yaml` 의 `done: merged` 로 고릅니다. 진행 화면 범례에 쓰는 규칙이 늘 적힙니다.

`merged+ci` 는 `.dropviz/ci-status.json` 을 먼저, 없으면 `origin` 의 GitHub API 를 읽기만 합니다(공개 저장소는 토큰이 필요 없고, `GITHUB_TOKEN`/`GH_TOKEN` 이 있으면 한도가 커짐). 다른 규칙은 네트워크를 쓰지 않습니다. 답은 커밋별로 `.dropviz/` 에 저장하고, 한 번에 토큰 없이 40번(있으면 400번)까지만 부르며 한도가 거의 바닥나면 멈춥니다. 검사를 읽지 못한 커밋은 `merged+ci` 에서 완료가 아니며 이유가 적힙니다. 다시 보기의 지난 날은 `merged` 규칙을 쓰고, 마지막 장면만 실제 지도입니다.

### 다시 보기는 자란다

아직 어떤 커밋도 닿지 않은 기능은 다시 보기에서 그려지지 않고 빈 칸(어둡게, 점선)으로 남았다가 첫 커밋 날에 채워집니다.
