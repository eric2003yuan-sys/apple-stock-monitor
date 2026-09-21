# apple-stock-monitor

个人用的小工具：定时查询 Apple Store 香港 6 家门店的取货库存（目前盯 iPhone 18 Pro Max 1TB 银色），
有货时通过 Bark / Server酱 提醒自己。只查询和提醒，不会下单。

- `check.py` — 查询 + 提醒逻辑（仅标准库）
- `.github/workflows/monitor.yml` — GitHub Actions 定时任务
- `state.json` — 运行状态，由工作流自动更新

推送密钥存放在仓库 Secrets（`BARK_KEY`、`SERVERCHAN_KEY`）中，不在代码里。
仓库变量 `SILENT=true` 时只弹无声横幅。
