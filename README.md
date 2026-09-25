# 燃气技能竞赛资格与裁评分离

管理燃气技能竞赛的资格、赛程资源、评分证据、回避与申诉。

## 参与方与事实

主要参与方包括参赛企业、竞赛选手、裁判人员、设备保障人员、赛事秘书处。领域资料记录以下已经确认的事实：

- 燃气行业竞赛有二十七支代表队和一百六十二名一线选手参加
- 竞赛设置燃气用户安装检修工和燃气管网运行工两个工种
- 赛事重点检验安全运行、风险防控、民生服务和应急处置能力

## 业务约束

- 选手资格
- 裁判回避
- 设备校准
- 赛题版本
- 重赛规则
- 奖项名额

`contracts/context.schema.json` 描述资料结构，`fixtures/context.json` 提供不含真实身份信息的示例，`src/competition_context.py` 负责读取和校验这些资料。

## 现场竞赛运行服务

`src/competition/` 在领域资料之上提供终评现场运行服务，所有处置写入只可追加的事件日志（JSONL），断电重启后按原顺序重放恢复：

- **报名冻结**：`freeze_registration` 固定代表队名额与选手资格；伤病替补沿用原名额槽位并记录赛区说明；资格更正只追加记录，最初材料保留在冻结快照与历史版本中。
- **赛程排布**：`generate_schedule` 依据工种、设备校准状态、选手时段与裁判回避关系排出可执行轮次，排不下任何一名选手即整体失败；`judge_task_list` 不下发本单位选手的评分任务。
- **裁评分离**：`record_score` 只接受排定裁判且无回避关系的评分；`verify_score` 拒绝录分者复核自己的结果；理论、实操、安全违规、应急处置分项计算。
- **设备故障**：`report_equipment_fault` 只使关联工位成绩进入待裁定，`decide_adjudication` 由赛事负责人逐条决定续赛、重赛或保留，系统不提供批量清空整轮数据的操作。
- **申诉与并列**：申诉期间冻结相关名次但不阻塞无关奖项；改判与并列处理必须说明影响的候选人，成绩原件保留为历史版本。
- **成绩发布**：`public_results` 只呈现获奖所需信息；`audit_report` 与 `audit_events` 供内部审计还原资格、工位、分项、回避与每次裁定。

```python
from src.competition import CompetitionService

service = CompetitionService.open("var/final/events.jsonl")  # 打开或创建事件日志
service.register_team("T1", "代表队一", {"燃气用户安装检修工": 2})
# ...报名、冻结、排程、录分、裁定、申诉...
service = CompetitionService.open("var/final/events.jsonl")  # 断电重启后重放恢复
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src
```

两条命令只读取仓库内文件，不需要连接外部业务系统。
