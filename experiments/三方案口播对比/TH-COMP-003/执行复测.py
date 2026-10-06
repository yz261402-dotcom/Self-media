"""开通后新一轮执行入口；复用原有已测试限次执行器，不重置旧账本。"""
from pathlib import Path
import importlib.util
import json

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_trial_runner', ROOT.parent / 'TH-COMP-002/补测火山.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
runner.ROOT = ROOT
runner.AUTH_ID = 'TH-COMP-003-user-omnihuman-service-enabled'

if __name__ == '__main__':
    try:
        runner.main()
    except Exception as error:
        print(json.dumps(runner.api.safe_error(error), ensure_ascii=False))
        raise SystemExit(2)
