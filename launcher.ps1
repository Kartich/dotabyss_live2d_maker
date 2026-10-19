$ErrorActionPreference = 'Stop'
$ToolDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ToolDir
$Log = Join-Path $ToolDir 'launcher_error.log'

function Find-Python {
    $candidates = @(
        @{Cmd='py'; Args=@('-3')},
        @{Cmd='python'; Args=@()},
        @{Cmd='python3'; Args=@()}
    )
    foreach ($c in $candidates) {
        try {
            & $c.Cmd @($c.Args) -c "import sys; print(sys.executable)" *> $null
            if ($LASTEXITCODE -eq 0) { return $c }
        } catch {}
    }
    return $null
}

try {
    $py = Find-Python
    if ($null -eq $py) {
        throw "未找到 Python 3。请安装 Python 3.10+，安装时勾选 Add Python to PATH。"
    }
    $script = Join-Path $ToolDir 'dot_maintainer.py'
    & $py.Cmd @($py.Args) $script
    $code = $LASTEXITCODE
    if ($code -ne 0) { throw "程序退出码: $code" }
} catch {
    $msg = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $($_.Exception.Message)`r`n$($_.ScriptStackTrace)"
    $msg | Out-File -FilePath $Log -Encoding UTF8
    Write-Host ''
    Write-Host 'Dot 离线资源维护器启动失败。' -ForegroundColor Red
    Write-Host $msg -ForegroundColor Yellow
    Write-Host "详细日志: $Log"
    Read-Host '按 Enter 关闭'
    exit 1
}
