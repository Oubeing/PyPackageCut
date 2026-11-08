# PyPackageCut

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](requirements.txt)

**精准、安全地为 Python 打包产物（PyInstaller / Nuitka / embeddable）瘦身。**

通过**进程级精确占用判定**识别“运行时真正被使用”的文件，把未被使用的文件移动到 `<文件夹>_new`，并可随时**一键无损还原**。从设计上做到：**宁可漏删，绝不误删**。

---

## 目录

- [核心特性](#核心特性)
- [实现原理](#实现原理)
- [快速开始](#快速开始)
- [操作流程（推荐）](#操作流程推荐)
- [模式说明](#模式说明)
- [白名单（white_files.json）](#白名单white_filesjson)
- [项目结构](#项目结构)
- [从源码运行 / 构建](#从源码运行--构建)
- [发布与分发](#发布与分发)
- [注意事项](#注意事项)
- [贡献](#贡献)
- [许可证](#许可证)

---

## 核心特性

1. **最精准的占用判定（进程级）**
   - 优先 `psutil`：枚举目标进程已加载模块（`memory_maps`）与已打开文件句柄（`open_files`），直接拿到目标程序真正在用的文件；
   - 回退 `ctypes`（Windows Toolhelp）：枚举目标进程已加载模块（`.pyd/.dll/.py/.pyc/内存映射文件`）。
   - 两种方式都直接读取“目标程序真正在用哪些文件”，比旧版“能否以写方式打开”的探测精准得多，从根本上避免**误删**。
   - **目标进程自动发现**：凡是引用了所选目录内文件的进程即视为目标；也可在“目标进程名(可选)”填写 `app.exe` 精确限定。
2. **支持多个目标文件夹**：目标项目常分布多处（如嵌入式 Python 在 `runtime`、第三方库在 `site-packages`），可一次性全部加入；各目录独立扫描 / 移动 / 还原，并自动剔除相互包含的重复目录。
3. **照顾 GUI 程序“全部功能走一遍需要时间”**
   - 新增**「持续监控(手动停止)」**：勾选后忽略秒数，持续采样直到你点“停止”。先勾选 → 开始预览/处理 → 在目标程序里从容点遍每个功能 → 确认全加载完 → 点“停止”生成结果；
   - 也可把“运行时监控”设长一点（如 30~60 秒），在期间操作各功能；
   - 任何“曾被打开/加载”的文件都会在监控窗口内被记录并保留，根治“懒加载导致误删”。
4. **分析预览（干跑）**：先列出“将移动”清单，**不会删除任何文件**，确认后再执行。
5. **一键还原**：依据各目录《`<目录名>_文件移动清单.txt`》把文件从 `<文件夹>_new` 无损还原。
6. **安全守卫**：若未检测到任何文件被目标程序占用，拒绝盲目删除并提示先运行目标程序。
7. **白名单 v2**：支持 glob、目录级保护、文件名精确匹配，避免 `used.dll` 误匹配 `unused.dll` 这类子串重叠。
8. **全局异常保护**：未捕获异常弹窗提示而非静默闪退。

---

## 实现原理

当 Python 程序运行时，它真正需要的文件会被系统加载/锁定（`.dll`/`.pyd` 由系统加载器加载、数据/资源文件被打开句柄持有）。本工具**直接枚举目标进程“正在使用”的文件**，与“未被使用”的文件对比，将后者移动到 `<文件夹>_new` 目录，并可随时还原。

---

## 快速开始

### 方式一：下载发布版（推荐）
从 [Releases](https://github.com/your-username/PyPackageCut/releases) 下载 `PyPackageCut.exe`，双击即可运行（无需安装 Python）。

### 方式二：从源码运行
```bash
pip install -r requirements.txt
python script/main.py
```

---

## 操作流程（推荐）

1. **添加目标文件夹**：启动程序，点“添加文件夹”，把 `runtime`、`site-packages` 等**全部**相关目录都加进去（可多个）。
2. **运行目标程序**：启动打包好的程序，开启全部功能，使其完全加载。
3. **设置监控**：GUI 程序推荐勾选**「持续监控(手动停止)」**；或把“运行时监控”设为 30~60 秒。可选填“目标进程名”精确限定。
4. **分析预览（不会删除任何文件）**：点“分析预览”，监控结束后核对“将移动”列表；若列表里有你确定需要的文件，编辑 `white_files.json` 后重新预览。
5. **开始处理**：确认无误后点“开始处理”，未占用文件按目录移动到各自的 `<文件夹>_new`，并生成 `<目录名>_文件移动清单.txt`。
6. **验证与还原**：再次运行目标程序验证；若异常，点“一键还原(全部目录)”即可按清单无损还原。

---

## 模式说明

- **安全模式（默认）**：仅处理 `.pyd` / `.dll`。这类文件由系统加载器以排他方式加载，进程级探测准确，成功率最高，**推荐优先使用**。
- **极限模式**：处理所有类型（含数据/资源文件）。瘦身更彻底，但部分文件可能被目标程序以“共享读”方式打开而难以被探测识别，误删风险更高，请务必配合“分析预览”与“一键还原”使用。

---

## 白名单（white_files.json）

首次运行自动生成 `white_files.json`，记录不移动的文件。支持三种写法：

- 文件名/目录名（如 `python.exe`、`docx/templates` 保护该目录下全部内容）；
- glob 通配（如 `*.json`）；
- 文件名精确匹配（仅在作为完整路径片段时生效，避免 `used.dll` 误匹配 `unused.dll` 这类子串重叠）。

按实际使用情况酌情增删，改完重新“分析预览”即可生效。

---

## 项目结构

```
PyPackageCut/
├── PyPackageCut.int          # 引导文件（设置路径并 import main）
├── script/                  # 源码目录
│   ├── cutcore.py           # 核心引擎（与界面解耦、纯标准库、可独立测试）
│   ├── main.py              # 主界面（GUI）
│   ├── main1.py             # 备选入口（带窗口图标）
│   └── white_files.json     # 运行时生成（已 gitignore）
├── tests/
│   └── test_cutcore.py      # 核心引擎自动化测试
├── build/
│   └── PyPackageCut.spec    # PyInstaller 打包配置
├── LICENSE                  # MIT
├── CHANGELOG.md
├── CONTRIBUTING.md
├── README.md
├── requirements.txt
└── pyproject.toml
```

> 注：`runtime/`、`site-packages/`、`PyPackageCut.exe`、`*_new/`、`*_文件移动清单.txt`
> 属于运行依赖与运行产物，已在 `.gitignore` 中排除，不纳入源码库。

---

## 从源码运行 / 构建

### 运行
```bash
pip install -r requirements.txt
python script/main.py
```

### 测试
```bash
pip install pytest
pytest
# 或：python tests/test_cutcore.py
```

### 构建为单文件 exe
```bash
pip install pyinstaller
pyinstaller build/PyPackageCut.spec
# 产物：dist/PyPackageCut.exe
```

---

## 发布与分发

项目支持两种分发方式：

1. **PyInstaller 单文件 exe**：如上构建，适合一般用户。
2. **引导式分发（仓库自带方式）**：将 `script/`、`runtime/`、`site-packages/`、`PyPackageCut.int` 与引导 exe 一同复制即可。
   其中 `PyPackageCut.int` 的内容为：
   ```python
   import sys, os
   os.chdir(os.path.dirname(__file__))
   sys.path.append(os.path.abspath('script'))
   import main
   main.main()
   ```
   即：设置工作目录 → 把 `script/` 加入模块搜索路径 → `import main`。
   因此**修改 `script/` 源码即生效，无需重新打包**。

---

## 注意事项

- 仅在 Windows 上测试（依赖 Windows 进程/句柄语义）。
- 文件默认是**移动**而非删除，且可一键还原，安全可控；请勿手动删除 `<文件夹>_new`。
- 若目标程序以“单文件(onedir/onefile)”方式运行，文件可能在临时目录而非所选文件夹，此时探测不到占用，脚本会拒绝盲目删除——请选择程序真正落盘的目录。
- 若未检测到任何文件被占用，脚本会拒绝盲目删除并提示先运行目标程序（这是安全守卫，不是故障）。
- 若 `python` 无法直接运行 `main.py` 源码，请从 Releases 页下载发布版。

---

## 贡献

欢迎任何形式的贡献！详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 许可证

本项目采用 [MIT 许可证](LICENSE)。
