# Security

Please report security problems **by e-mail to zeus@zeus.kim**, not in a public issue.
Include what you did, what you saw, your OS and `dropviz --version`. You will get an answer by e-mail.

Scope: dropviz runs locally. `serve` binds to 127.0.0.1 only and rejects other Host headers; it reads your git
repository and, for `tokens`, local AI-tool records (counts only are kept in its cache). It sends nothing anywhere.

---

# 보안

보안 문제는 공개 이슈가 아니라 **zeus@zeus.kim 으로 이메일**해 주세요. 한 일, 본 것, OS, `dropviz --version` 을 적어 주세요.
