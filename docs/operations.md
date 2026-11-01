# 日常操作与恢复

操作前确认逻辑节点和普通管理员身份，sudo密码只在本地终端输入。先检查构建/发布/备份是否空闲，保留独立救援连接。

| 节点 | 只读核查 | 维护启停 |
|---|---|---|
| web01-hz | Nginx/exporter active；nginx -t；固定current Compose ps/config；80/18080 healthz/version | 对current/compose.json执行Compose stop或up -d --no-build --pull never；Nginx按维护窗口stop/start，单实例会中断 |
| ops01 | Jenkins、只读Git、发布path active；构建/请求/执行器空闲；free/df | 先停path，等发布与构建结束，再停Jenkins；启动先Git/Jenkins，健康后启path |
| monitor01 | 六服务active；六up1/probe1；ALERTS/AM alerts为空；Grafana健康 | 维护时按需停数据/告警服务，启动先采集隧道/exporter/blackbox，再Prometheus/Grafana/AM；停止期间没有监控样本 |

Jenkins/Grafana通过回环SSH隧道访问，不向公网开放8080/3000/9090等管理端口。Jenkins URL绑定管理隧道本地端口，变更端口时应核对绝对链接。

## 排障路径

- SSH失败：检查既定私网路径、主机身份与精确来源限制，停止依赖操作；不回退旧公网、不禁用校验。
- HTTP502：Nginx→回环上游→容器状态→镜像/revision/version。使用正式rollback入口恢复保存健康版本，不手改current.json。
- 发布失败：对照attempt、真实版本和两个入口。recovery=succeeded不改变失败构建结果；恢复也失败时保留失败现场和救援入口。
- Jenkins匿名403：属于权限控制，使用已登录界面或root白名单采集器。最终结果读取build.xml根级result，不能匹配嵌套checkout的SUCCESS。
- Grafana空图：检查up、数据源、时间范围、序列和隧道。配置文件与当前认证API均须验证；不依赖旧截图。
- 通知计数：Alertmanager重启会复位进程计数，应分窗口记录；计数不能代替实际收件。
- 内存或磁盘不足：暂停新重型任务，保留已知健康镜像和备份；不自动prune恢复镜像，不擅自扩VM或改变分区。

## 故障演练边界

Nginx、应用、采集隧道和坏健康版本演练采用单项注入、有界观察及独立恢复保护。运行前检查保存镜像、恢复SHA、当前健康、监控和救援入口。默认只读预检；--execute会真实中断单实例服务。固定SHA驱动只适用于对应已审查环境，不能随意替换目标。

CPU压力与隔离磁盘演练没有完成，不能通过填根盘或取消安全限制来补齐。此仓库没有生产HA、零停机或容量性能收益承诺。

## 费用与资源检查

实例计费方式、到期、算力包余量、公网带宽/流量、云盘/快照、公网IP保留费和自动续费都要登录控制台核对。停机是否仍收费以实际产品和账单为准，不能推算免费额度。仓库不自动续费、停云机或释放资源；没有当前账单/到期证据时明确待核对。
