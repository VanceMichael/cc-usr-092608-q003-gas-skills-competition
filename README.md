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

`src/competition/` 提供现场竞赛运行服务，全部状态变化写入只增事件日志（`journal.jsonl`），断电重启后按原顺序重放恢复；任何更正只追加，不抹掉最初材料。

- `models.py`：工种、分项（理论题、实际操作、安全违规扣分、应急处置）、设备状态、成绩状态、裁定与申诉的领域模型
- `journal.py`：追加式事件日志，写入即落盘
- `scheduling.py`：按工种、设备校准状态、选手时段与裁判回避关系排出可执行轮次
- `ranking.py`：名次排列与并列扩额分配
- `service.py`：运行服务主体
  - 报名截止固定代表队名额与选手资格；伤病替补沿用原名额并留存赛区说明
  - 裁评分离：裁判看不到本单位选手的评分任务，录分者不能复核自己的结果
  - 设备故障仅使关联工位成绩进入待裁定，由赛事负责人逐工位决定续赛、重赛或保留，不批量清空整轮数据
  - 申诉期间冻结相关名次但不阻塞无关奖项；并列处理与改判均记录影响的候选人
  - 对外成绩单只呈现获奖所需信息；内部审计可还原资格、工位安排、评分分项、回避记录与每次裁定

```python
from datetime import datetime
from src.competition import CompetitionService, Trade, Component, Qualification

service = CompetitionService.create("var/final-2026")
service.register_team("T1", "甲队", {Trade.INSTALL_REPAIR: 1})
service.register_contestant(
    "C1", "选手一", "T1", Trade.INSTALL_REPAIR,
    Qualification("地方选拔第一名", "一线岗位五年", "培训合格"), slots=[1, 2],
)
# ……报名截止、排程、评分、裁定、申诉、终评
service = CompetitionService.open("var/final-2026")  # 断电重启后恢复
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
