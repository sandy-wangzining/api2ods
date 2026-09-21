## 改了什么

<!-- 一到两句话。修 bug 请写"原来会怎样、现在怎样" -->

## 为什么

<!-- 复现步骤 / 遇到的作业 / 关联 Issue。数据类问题请写清"原来会丢数还是写错分区" -->

Closes #

## 怎么验证的

<!-- 跑了哪些用例、有没有用真实作业端到端验证过（贴脱敏后的关键日志） -->

- [ ] `python -m unittest discover -s tests` 全过
- [ ] `ruff check .` 0 告警
- [ ] 涉及数据完整性的改动，补了"改之前会失败"的回归用例

## 自查

- [ ] 没提交密钥（`jobs/*.json`、`config.json`、`signers.py` 都在 `.gitignore` 里）
- [ ] 日志/异常里的敏感值都过了 `redact()`
- [ ] 失败路径不会让分区停在一半（写前校验仍在 `delete_partition` 之前）
- [ ] 改了配置项/命令行参数 → 同步更新了 `README.md` 与 `jobs/_template_full.example.json`
- [ ] 行为变化已写进 `CHANGELOG.md`
