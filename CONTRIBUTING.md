# 贡献指南 / Contributing

感谢你考虑为 PyPackageCut 做出贡献！

## 开发环境
```bash
# 1. 克隆仓库
git clone https://github.com/your-username/PyPackageCut.git
cd PyPackageCut

# 2. 创建虚拟环境（可选）
python -m venv venv
venv\Scripts\activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 从源码运行
python script/main.py
```

## 运行测试
```bash
# 使用 pytest（推荐）
pip install pytest
pytest

# 或直接运行（无需 pytest）
python tests/test_cutcore.py
```

## 代码规范
- 核心逻辑放在 `script/cutcore.py`（与界面解耦、纯标准库、可独立测试）。
- 界面逻辑放在 `script/main.py` / `script/main1.py`。
- 提交前请确保 `python -m py_compile script/*.py` 通过，且测试通过。
- 保持“宁可漏删、绝不误删”的安全优先原则。

## 提交 PR
1. Fork 本仓库并创建特性分支：`git checkout -b feature/your-feature`
2. 提交改动并编写清晰的提交信息
3. 确保测试通过
4. 发起 Pull Request，描述改动动机与验证方式

## 问题反馈
如有 Bug 或改进建议，请在 Issues 中描述：环境（Windows 版本 / Python 版本）、复现步骤、日志与预期结果。
