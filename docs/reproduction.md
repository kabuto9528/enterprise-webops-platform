# 配置和复现

## 本地应用与测试

需要Git及Python3.12。在独立检出目录运行，不覆盖已部署目录：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest tests -q
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Windows用 `.venv/Scripts/python.exe`。Linux专属的权限、flock、链接和发布工具测试在Windows会跳过。停止前台应用按Ctrl+C；另开终端核对 `/healthz` 和 `/version`。开发默认版本为dev，部署时注入完整提交SHA。

## 管理员需要准备的输入

- CentOS Stream10/x86_64节点、Python3.12、Git、systemd、OpenSSH和已授权sudo。资源创建、系统基线和SSH授权由管理员完成，仓库不自动创建云资源。
- 参考ansible下的example inventory，私下填写管理地址、普通管理员、Linux独立密钥、严格known_hosts、经审查的Nginx摘要、监控目标与精确来源。example.invalid与REPLACE_*不可直接用于部署。真实inventory放 `.local/` 或 `inventory*.local.yml`。
- 密钥在对应节点生成，仅分发公钥。Windows PEM不复制到Linux；不关闭主机校验，不扩大端口或来源范围。ECS使用既定Tailscale私网SSH，现有Windows入口别名为webops。
- 按lock文件从可信来源准备组件包、Jenkins RPM/签名密钥/插件、固定digest基础镜像及14个Linux wheel；核对名称、长度、全部SHA256。源码仓库不包含依赖二进制。

## 配置验证

控制环境单独安装ansible/requirements-control.txt，设置ANSIBLE_CONFIG为本仓库ansible/ansible.cfg，设置ANSIBLE_SSH_USETTY=False；保留StrictHostKeyChecking。用example inventory对base/configure/adopt/deploy/rollback及monitoring入口执行syntax-check。语法检查不连接目标，不能代替真实幂等和运行验收。

## 部署顺序

1. 核对whoami、hostname、os-release、python/git版本、资源与独立救援连接。资源不足或采集链路不稳定时停止变更。
2. 用configure及角色准备Docker/Nginx/目录/发布引擎，实际运行两次，第二次应changed0。新环境先验证首个健康容器再adopt；已有current时不能删元数据伪造首次接管。
3. 用 `ansible/deploy.yml -e release_sha=<已审查完整SHA>` 发布，用 `ansible/rollback.yml -e release_sha=<已保存健康SHA>` 回滚。核对镜像ID、OCI revision、两个HTTP入口与current，而非只看元数据。
4. 监控先exporter和来源约束，再专用SSH采集隧道、Prometheus/Grafana。准备stack_assets中经锁定校验的组件包及私有监控参数，执行monitoring相关playbook；分别验证六up1/probe1、面板和重启后的历史持久化。
5. Alertmanager默认unconfigured。经用户明确授权收件人后，在目标本地终端用stage09-configure-email.py隐藏输入SMTP凭据；配置检查通过后，还需故障/恢复真实投递及实际收件。Grafana密码只在目标生成和保存。
6. Jenkins安装工具使用锁定RPM和插件，不包含磁盘扩容。stage10-prepare-bundle.py从经核验的本地下载缓存生成安装包；stage10-install.sh要求匹配的ops01节点、至少2GiB安装空闲空间和已审查的运行资源。安装并不授权改变分区。该门槛不是生产容量建议。
7. 审查Jenkinsfile、jenkins/ci-executor.*及固定root执行器，准备节点私有inventory和可信Git bare。先手动健康构建，再验证SCM轮询。失败构建保持FAILURE，即使前版恢复成功。

部分stage命名脚本是原三节点环境的安装/验收工具，包含固定主机名、目录或已验收SHA防误用条件；它们不是通用新机一键安装器。尤其stage11-resume和故障驱动必须逐项适配、审查恢复路径后再运行。优先使用声明式角色及明确参数，不绕过保护条件。

## 备份与隔离恢复

在对应项目节点本地运行 `sudo python3 scripts/stage13-backup-restore.py`。备份含秘密，仅留root受限目录；脚本在独立目录实际恢复并核对内容、权限、属主、链接、配置及SQLite，原环境保留。Jenkins/监控数据服务短停时有5分钟独立重启保护；先确保没有构建和维护冲突。

该工具不备份OS/RPM、VM磁盘或Docker镜像层，没有新机全服务接管与异地灾备结论。恢复副本不启动第二套外发通知服务。真实覆盖恢复须另行确认目标、保护残存数据并核对依赖。
