// TeamCity 配置即代码（Kotlin DSL）。
//
// 启用方式见 .teamcity/README.md：在 TeamCity 里给项目打开 Versioned Settings，
// 指向本仓库、格式选 Kotlin，之后每次提交都会自动同步下面的配置。
//
// 设计：测试流水线随提交自动跑；发布流水线只手动点（或你显式打开末尾的
// finish-build trigger），避免每次提交都把新 EXE 推给全体用户。

import jetbrains.buildServer.configs.kotlin.*
import jetbrains.buildServer.configs.kotlin.FailureAction
import jetbrains.buildServer.configs.kotlin.buildFeatures.xmlReport
import jetbrains.buildServer.configs.kotlin.buildSteps.script
import jetbrains.buildServer.configs.kotlin.triggers.vcs
import jetbrains.buildServer.configs.kotlin.vcs.GitVcsRoot

project {
    vcsRoot(StockChartLocal)

    buildType(Tests)
    buildType(Release)
}

// 本机仓库（服务器与代理同机，直接读工作副本路径）
object StockChartLocal : GitVcsRoot({
    name = "stockchart-local"
    url = "C:/Users/ASUS/.qoderworkcn/workspace/mqxnujmu7xwpkdh9"
    branch = "refs/heads/main"
    branchSpec = "+:refs/heads/main"
})

// ── 测试流水线：git commit 后自动跑全量 pytest ──────────────────────────────
object Tests : BuildType({
    name = "测试（pytest）"
    description = "每次提交跑全量 pytest；失败即红，测试结果在 Tests 标签页可查"

    vcs {
        root(StockChartLocal)
    }

    params {
        // 代理跑在 LocalSystem 下，PATH 里未必有系统 Python：显式指定带 pytest 的解释器
        text("env.STOCKCHART_TEST_PYTHON",
             "C:/Users/ASUS/AppData/Local/Programs/Python/Python314/python.exe",
             label = "测试用 Python", allowEmpty = true)
    }

    steps {
        script {
            name = "pytest"
            scriptContent = "pwsh -NoProfile -ExecutionPolicy Bypass -File scripts/ci/run_tests.ps1"
        }
    }

    features {
        xmlReport {
            reportType = XmlReport.XmlReportType.JUNIT
            rules = "build/reports/pytest.xml"
        }
    }

    triggers {
        vcs {
            branchFilter = "+:<default>"
        }
    }

    artifactRules = "build/reports/*.xml"
})

// ── 发布流水线：构建 → 签名 → 出清单 → 上传 ────────────────────────────────
// 默认手动 Run。发版会立刻影响所有已安装客户端，不要挂到每次提交上。
object Release : BuildType({
    name = "发布（构建+签名+上传）"
    description = "构建 EXE、代码签名、生成带签名的 update.json 并上传到 data.hsdc.cloud"

    vcs {
        root(StockChartLocal)
    }

    // 发布前必须有一次同提交的绿色测试；测试红了直接不发
    dependencies {
        snapshot(Tests) {
            onDependencyFailure = FailureAction.FAIL_TO_START
        }
    }

    params {
        // 发布在本地工作副本里跑：构建用的 .venv-win11 / win7 python 运行时不在 git 里，
        // 代理的干净检出目录里没有它们。这里直接指向本机仓库。
        text("repo.path", "C:/Users/ASUS/.qoderworkcn/workspace/mqxnujmu7xwpkdh9",
             label = "本地仓库路径")

        // 更新说明（显示在客户端版本徽章里）
        text("release.notes", "", label = "更新说明",
             description = "写进 update.json 的 notes，显示在版本徽章提示里",
             allowEmpty = true)
        // 同一天第二次发界面包时必须抬一位，如 2026.10.05.2；留空 = 跟构建号一致
        text("release.ui_version", "", label = "界面包版本（可选）",
             description = "同一天重复发界面时必须抬一位，否则客户端不会重新下载",
             allowEmpty = true)

        // 签名材料（不进 git）：路径是公开信息，口令必须是 Password 类型
        text("env.STOCKCHART_SIGN_PFX",
             "C:/ProgramData/StockChart/secrets/stockchart-codeSigning.pfx",
             label = "代码签名 PFX 路径", allowEmpty = true)
        password("env.STOCKCHART_SIGN_PFX_PASSWORD", "",
                 label = "PFX 口令",
                 description = "TeamCity 会打码；不要写进脚本或普通参数")
        text("env.STOCKCHART_SIGN_SUBJECT",
             "CN=HSDC StockChart Code Signing, O=HSDC",
             label = "证书主题", allowEmpty = true)

        // 上传用的 SSH 私钥
        text("env.STOCKCHART_PUBLISH_KEY",
             "C:/ProgramData/StockChart/secrets/id_ed25519_ubuntu",
             label = "发布用 SSH 私钥路径", allowEmpty = true)
        text("env.STOCKCHART_PUBLISH_HOST",
             "ubuntu@81.71.134.71",
             label = "发布服务器", allowEmpty = true)
    }

    steps {
        script {
            name = "构建 + 签名 + 生成清单 + 上传"
            scriptContent = "pwsh -NoProfile -ExecutionPolicy Bypass -File \"%repo.path%/scripts/ci/package.ps1\" -Notes \"%release.notes%\" -UiVersion \"%release.ui_version%\" -Publish"
        }
    }

    artifactRules = "update_dist/*"

    // 想让"测试绿了自动发版"，取消下面注释（务必先想清楚：每次提交都会推新包）
    //
    // triggers {
    //     finishBuildTrigger {
    //         buildType = "${Tests.id}"
    //         successfulOnly = true
    //         branchFilter = "+:<default>"
    //     }
    // }
})
