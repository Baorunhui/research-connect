# 模块来源记录

本文件记录首次建立单仓库时导入的代码来源。仓库采用源码快照，不使用 Git submodule；后续同步上游时必须单独提交并记录新版本。

| 目录 | 来源 | 导入版本 |
|---|---|---|
| `apps/connect-hub` | 本工作区 `connect-hub/` | 2026-08-29 工作区快照 |
| `modules/xhs-agent` | 本工作区 `xhs_agent/` | 2026-08-29 工作区快照；原仓库尚无提交 |
| `modules/daily-paper-reader` | `https://gitee.com/lp18026522720/daily-paper-reader.git` | `a5a6541aaa766949a836f2e57bce23d9dd10bad8` |
| `modules/citationclaw` | 用户提供的 `CitationClaw-20260829.tar.gz` | 2026-08-29 归档快照 |

导入时没有复制 `.git`、`.venv`、`.env`、SQLite、运行结果、缓存和本机配置。CitationClaw 的 `config.json`、`dot_citationclaw/` 以及包含真实 Key 的内部说明文件也未进入仓库。

