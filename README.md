# Enterprise WebOps Platform

三节点、单实例的Linux运维实践项目，覆盖Git → Jenkins → Ansible → Docker/FastAPI → Nginx，以及Prometheus/Grafana/Alertmanager监控、告警、故障恢复和备份恢复。

本仓库发布应用、自动化配置、模板、版本锁、测试和项目说明。学习日志、内部阶段文档、原始运行证据、截图、真实inventory、连接信息、密钥和备份不上传。

## 架构与文档

- [架构、版本和权限](docs/architecture.md)
- [环境准备与复现步骤](docs/reproduction.md)
- [日常操作、故障恢复与费用检查](docs/operations.md)
- [已验证结果与能力边界](docs/validation.md)

ops01承载Git/Jenkins/Ansible，web01-hz承载Nginx和Docker应用，monitor01承载采集、图表及告警。使用逻辑名称描述拓扑；实际地址和凭据由管理员私下准备。

## 快速运行

需要Python3.12和Git。在检出目录的PowerShell执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Linux使用 `.venv/bin/python`，可执行 `pytest tests -q` 运行Linux专属发布/权限/恢复测试。另开终端访问 `/healthz` 和 `/version`，停止前台应用按Ctrl+C。

| 接口 | 说明 |
|---|---|
| `/` | 服务标识 |
| `/healthz` | 当前进程可响应，无数据库/外部依赖检查 |
| `/version` | 启动时WEBOPS_VERSION，开发默认dev，发布注入完整提交SHA |

应用无公开故障控制入口、数据库或交易业务。不存在路径返回404；无效版本值拒绝启动。

## 目录

| 路径 | 内容 |
|---|---|
| app/、tests/ | 最小应用及发布引擎、CI权限、Git只读入口、恢复工具测试 |
| ansible/ | 配置/发布/回滚及监控角色，example inventory |
| docker/、Dockerfile | 固定基础镜像与runtime wheel哈希锁、受限运行配置 |
| jenkins/、Jenkinsfile | core/plugins锁、Groovy引导、固定执行器单元与Pipeline |
| monitoring/ | exporter/监控组件锁、告警规则测试、Grafana provisioning和七面板 |
| scripts/ | 发布/CI引擎、经过约束的安装/核验、故障与文件恢复工具 |
| nginx/、systemd/ | Nginx与历史宿主机应用单元；当前应用使用Docker |

## 验证与限制

2026-11-01已核验：Linux69项隔离测试、10个Ansible入口语法检查，真实Jenkins健康发布、健康版本正式回滚、四项故障及故障/恢复邮件实际收件、三节点文件和选定数据的隔离恢复。详情和限制见验证摘要。

这是单实例学习环境。没有生产高可用、零停机、性能收益或整机/异地灾备结论；CPU压力与隔离磁盘演练未完成。宿主有实际内存余量风险。云费用、算力包余额和到期必须查控制台，不承诺免费额度。

管理端口保持回环，SSH采用已核验的主机身份和精确来源。Jenkins无sudo/wheel/docker和目标管理密钥；可信main写权限仍等于高权限发布授权，禁止接入不可信PR。仓库推送不会自动部署到当前ops01环境。

stage命名工具保留原节点/目录/版本防误用条件；应阅读脚本并按环境适配，不能作为从空账号创建基础设施的一键安装器。Jenkins安装只检查安装空间，不执行磁盘扩容。依赖二进制按锁文件另行下载核验。
