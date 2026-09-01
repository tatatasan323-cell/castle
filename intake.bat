@echo off
rem intake.bat  見張り ── 置き場を一巡して取り込み、画面まで作り直す。
rem タスクスケジューラから毎営業日1回、これだけを呼ぶ。
rem
rem 【1】多段の起動チェーンにしない。
rem   vbs や隠しcmd を挟むと、常駐のウイルス対策が無言で止めることがある。
rem   ここは python を直接呼ぶ1段に留める。
rem
rem 【2】ファイル名はASCII。
rem   日本語名の .bat は、呼び出し側の文字コード次第で「見つからない」と言われる。
rem
rem 【3】このファイル自身はCP932で保存する。
rem   cmd はバッチファイルをOEMの文字コードで読む。UTF-8で保存すると日本語の
rem   コメントが化け、その断片をコマンドとして実行しようとして落ちる
rem   （chcp を先に書いても、ファイルの読み方には間に合わない）。
rem
rem 【4】ログへ書く行はASCIIだけにする。
rem   python の出力はUTF-8。ここで日本語をechoすると、CP932とUTF-8が
rem   1つのログに混ざり、どちらの文字コードで開いても読めなくなる。
rem   読めないログは、無いログと同じ。
rem
rem 終了コード 0＝全部取り込めた ／ 0以外＝保留あり。
rem タスクスケジューラの「前回の実行結果」に出るので、そこで気づける。

setlocal
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
python "castle\app\intake.py" >> "instance\intake.log" 2>&1
set CODE=%ERRORLEVEL%
rem リダイレクトを先に置く。末尾に置くと「exit=1>>」の 1 が
rem ファイル記述子の指定と解釈され、数字がログから消える。
>> "instance\intake.log" echo [%DATE% %TIME%] intake exit=%CODE%
exit /b %CODE%
