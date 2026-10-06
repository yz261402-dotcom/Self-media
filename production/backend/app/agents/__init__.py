"""Agent 层：契约、角色与工具权限、模型后端、总控编排。

当前状态（对应 05 计划 T00，未验收）：
- 编排器为业务层确定性编排（app/agents/supervisor.py），模型默认使用离线确定性替身。
- deepagents 0.7.17 / langgraph 1.2.12 已安装，框架运行时（真实委派、interrupt、checkpoint）
  仍在试验中，未作为默认执行路径。见 app/agents/framework.py。
"""
