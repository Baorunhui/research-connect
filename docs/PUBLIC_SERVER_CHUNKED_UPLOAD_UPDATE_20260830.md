# Report Hub 分片整站上传更新（2026-08-30）

## 这次更新解决什么

Daily Paper 原版网页会长期保存 Markdown、图片和历史结果。本机当前站点压缩后约
80 MiB，单次 `PUT` 容易被 Nginx/Caddy/学校出口的请求体限制拦截，即使 Report Hub
应用自身允许 256 MiB，也可能出现 413 或连接中断。

本次增加分片上传协议：

- Connect Hub 将大于 4 MiB 的 ZIP 切成约 4 MiB/片；
- 每片携带 SHA-256，失败最多重试三次；
- Report Hub 收齐全部分片后校验并解压；
- 最终站点通过临时目录原子替换，上传过程中旧网页仍可正常访问；
- 未完成的分片不会覆盖线上站点。

新增接口：

```text
PUT /api/v1/sites/{site_id}/uploads/{upload_id}/parts/{part_number}?total_parts=N
X-Chunk-SHA256: <hex digest>
Content-Type: application/octet-stream
```

## Docker 服务器需要做什么

在公网服务器的 `research-connect` 仓库执行：

```bash
git pull origin main
docker compose build report-hub
docker compose up -d report-hub
docker compose logs --tail=100 report-hub
```

如果 Compose 服务名不同，以现有部署文件中的 Report Hub 服务名为准。无需迁移
SQLite，也不要删除 Report Hub 数据卷；公网 URL 和站点 token 会保持不变。

反向代理仍建议允许单片至少 8 MiB：

```nginx
client_max_body_size 8m;
```

分片模式不再要求代理接受完整的 80～200 MiB 请求。

## 更新后验证

本机重新启动 Connect Hub 时会进行原版网页补同步。成功日志类似：

```text
published daily-paper original site at https://.../s/<token>/
```

随后访问原有 Daily Paper URL，检查侧边栏和最新日报，无需生成新的日报。
