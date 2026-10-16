# StockChart 兼容文件说明

## Windows 7 兼容方案

### 问题

- WebView2 不支持 Windows 7
- 需要回退方案

### 解决方案

已创建 `启动.bat` 批处理文件，会自动：
1. 检测操作系统版本
2. Windows 10+ → 使用原生 exe（WebView2）
3. Windows 7 → 使用浏览器兼容模式

### 文件说明

| 文件 | 说明 |
|------|------|
| `StockChart.exe` | 主程序（Win10+，WebView2） |
| `launcher_win7.py` | Win7兼容启动器 |
| `启动.bat` | 一键启动（自动检测系统） |
| `WIN7_COMPAT.md` | 详细兼容方案说明 |

### Win7 用户使用方法

1. 双击 `启动.bat`
2. 程序会自动用默认浏览器打开
3. 或手动运行 `launcher_win7.py`

### 注意事项

- Win7 下界面在浏览器中打开，非独立窗口
- 建议安装 Chrome/Firefox 以获得最佳体验
- AI预测功能正常使用