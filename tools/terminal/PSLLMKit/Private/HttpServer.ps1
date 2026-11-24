# Private/HttpServer.ps1 - Multi-instance REST API server
# Each instance is a dedicated server for ONE app (claude, codex, gemini, droid).
# Runs in a separate runspace so the PowerShell prompt stays free.

$script:ServerInstances = @{}

# ---------------------------------------------------------------------------
# Server script that runs inside each background runspace
# ---------------------------------------------------------------------------
$script:ServerScriptBlock = {
    param($App, $Port, $ModulePath, $StopFlag, $SharedState)

    Import-Module $ModulePath -Force

    $listener = [System.Net.HttpListener]::new()
    $listener.Prefixes.Add("http://localhost:${Port}/")
    $listener.Start()

    try {
        while (-not $StopFlag.Stop) {
            # Async accept with 500 ms poll so we can check the stop flag
            $iar = $listener.BeginGetContext($null, $null)
            while (-not $iar.AsyncWaitHandle.WaitOne(500)) {
                if ($StopFlag.Stop) { return }
            }
            $context = $listener.EndGetContext($iar)

            try {
                $req = $context.Request
                $res = $context.Response

                # CORS
                $res.Headers.Add('Access-Control-Allow-Origin',  '*')
                $res.Headers.Add('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                $res.Headers.Add('Access-Control-Allow-Headers', 'Content-Type')

                if ($req.HttpMethod -eq 'OPTIONS') {
                    $res.StatusCode = 204
                    $res.Close()
                    continue
                }

                $path       = $req.Url.AbsolutePath.TrimEnd('/')
                $body       = $null
                $statusCode = 200

                switch ($path) {

                    '/api/health' {
                        $body = @{
                            status = 'running'
                            app    = $App
                            port   = $Port
                        } | ConvertTo-Json
                    }

                    '/api/status' {
                        $uptime = [DateTime]::UtcNow - [DateTime]$SharedState.StartTime
                        $body = @{
                            app       = $App
                            port      = $Port
                            running   = $true
                            uptime    = '{0:hh\:mm\:ss}' -f $uptime
                            sessionId = $SharedState.SessionId
                        } | ConvertTo-Json
                    }

                    '/api/session/reset' {
                        if ($req.HttpMethod -ne 'POST') {
                            $statusCode = 405
                            $body = @{ error = 'Use POST method.' } | ConvertTo-Json
                        }
                        else {
                            $oldId = $SharedState.SessionId
                            $SharedState.SessionId = $null
                            $body = @{
                                message      = 'Session reset'
                                oldSessionId = $oldId
                                newSessionId = $null
                            } | ConvertTo-Json
                        }
                    }

                    '/api/prompt' {
                        if ($req.HttpMethod -ne 'POST') {
                            $statusCode = 405
                            $body = @{ error = 'Use POST method.' } | ConvertTo-Json
                        }
                        else {
                            $reader  = [System.IO.StreamReader]::new(
                                $req.InputStream, $req.ContentEncoding)
                            $jsonStr = $reader.ReadToEnd()
                            $reader.Close()

                            $payload = $jsonStr | ConvertFrom-Json

                            if (-not $payload.prompt) {
                                $statusCode = 400
                                $body = @{ error = 'Missing "prompt" field.' } |
                                    ConvertTo-Json
                            }
                            else {
                                try {
                                    $result = Invoke-CLITool `
                                        -Provider     $App `
                                        -Prompt       $payload.prompt `
                                        -Model        $payload.model `
                                        -SystemPrompt $payload.systemPrompt `
                                        -SessionId    $SharedState.SessionId

                                    # Update session ID if tool returned one
                                    if ($result.sessionId) {
                                        $SharedState.SessionId = $result.sessionId
                                    }

                                    $body = @{
                                        content   = $result.content
                                        app       = $App
                                        model     = $result.model
                                        sessionId = $SharedState.SessionId
                                        duration  = $result.duration
                                    } | ConvertTo-Json -Depth 10
                                }
                                catch {
                                    $statusCode = 500
                                    $body = @{ error = $_.Exception.Message } |
                                        ConvertTo-Json
                                }
                            }
                        }
                    }

                    default {
                        $statusCode = 404
                        $body = @{
                            error     = "Not found: $path"
                            endpoints = @(
                                'GET  /api/health'
                                'GET  /api/status'
                                'POST /api/prompt'
                                'POST /api/session/reset'
                            )
                        } | ConvertTo-Json
                    }
                }

                $res.StatusCode  = $statusCode
                $res.ContentType = 'application/json; charset=utf-8'
                $buf = [System.Text.Encoding]::UTF8.GetBytes($body)
                $res.ContentLength64 = $buf.Length
                $res.OutputStream.Write($buf, 0, $buf.Length)
            }
            catch {
                try {
                    $errJson = @{ error = $_.Exception.Message } | ConvertTo-Json
                    $buf     = [System.Text.Encoding]::UTF8.GetBytes($errJson)
                    $context.Response.StatusCode      = 500
                    $context.Response.ContentType     = 'application/json; charset=utf-8'
                    $context.Response.ContentLength64  = $buf.Length
                    $context.Response.OutputStream.Write($buf, 0, $buf.Length)
                } catch {}
            }
            finally {
                try { $context.Response.Close() } catch {}
            }
        }
    }
    finally {
        $listener.Stop()
        $listener.Close()
    }
}

# ---------------------------------------------------------------------------
# Internal start / stop / status
# ---------------------------------------------------------------------------

function Start-HttpServerInternal {
    param(
        [Parameter(Mandatory)][string]$App,
        [int]$Port = 8080
    )

    if ($script:ServerInstances.Contains($App)) {
        throw "Instance for '$App' already running on port $($script:ServerInstances[$App].Port). Stop it first."
    }

    # Check port not in use by another instance
    foreach ($key in $script:ServerInstances.Keys) {
        if ($script:ServerInstances[$key].Port -eq $Port) {
            throw "Port $Port already in use by '$key' instance."
        }
    }

    $stopFlag    = [hashtable]::Synchronized(@{ Stop = $false })
    $sharedState = [hashtable]::Synchronized(@{
        SessionId = $null
        StartTime = [DateTime]::UtcNow.ToString('o')
    })
    $modulePath = (Resolve-Path (Join-Path $PSScriptRoot '..' 'PSLLMKit.psd1')).Path

    $runspace = [runspacefactory]::CreateRunspace()
    $runspace.Open()

    $ps = [powershell]::Create()
    $ps.Runspace = $runspace
    [void]$ps.AddScript($script:ServerScriptBlock)
    [void]$ps.AddParameter('App',         $App)
    [void]$ps.AddParameter('Port',        $Port)
    [void]$ps.AddParameter('ModulePath',  $modulePath)
    [void]$ps.AddParameter('StopFlag',    $stopFlag)
    [void]$ps.AddParameter('SharedState', $sharedState)

    $asyncHandle = $ps.BeginInvoke()

    $script:ServerInstances[$App] = @{
        App         = $App
        Port        = $Port
        Runspace    = $runspace
        PowerShell  = $ps
        AsyncHandle = $asyncHandle
        StopFlag    = $stopFlag
        SharedState = $sharedState
        StartTime   = [DateTime]::UtcNow
        Running     = $true
    }
}

function Stop-HttpServerInternal {
    param([Parameter(Mandatory)][string]$App)

    if (-not $script:ServerInstances.Contains($App)) {
        Write-Warning "No instance running for '$App'."
        return
    }

    $inst = $script:ServerInstances[$App]
    $inst.StopFlag.Stop = $true
    Start-Sleep -Milliseconds 800

    try {
        $st = $inst.PowerShell.InvocationStateInfo.State
        if ($st -notin @('Completed', 'Stopped', 'Failed')) {
            $inst.PowerShell.Stop()
        }
        $inst.PowerShell.Dispose()
    } catch {}

    try {
        $inst.Runspace.Close()
        $inst.Runspace.Dispose()
    } catch {}

    $script:ServerInstances.Remove($App)
}

function Stop-AllHttpServers {
    $apps = @($script:ServerInstances.Keys)
    foreach ($app in $apps) {
        Stop-HttpServerInternal -App $app
    }
}

function Get-HttpServerStatus {
    param([string]$App)

    if ($App) {
        if (-not $script:ServerInstances.Contains($App)) {
            return @{ App = $App; Running = $false }
        }
        $inst   = $script:ServerInstances[$App]
        $uptime = [DateTime]::UtcNow - $inst.StartTime
        return @{
            App       = $App
            Running   = $true
            Port      = $inst.Port
            StartTime = $inst.StartTime
            Uptime    = $uptime
            SessionId = $inst.SharedState.SessionId
            Url       = "http://localhost:$($inst.Port)"
        }
    }

    # Return all instances
    $result = @()
    foreach ($key in $script:ServerInstances.Keys) {
        $inst   = $script:ServerInstances[$key]
        $uptime = [DateTime]::UtcNow - $inst.StartTime
        $result += @{
            App       = $key
            Running   = $true
            Port      = $inst.Port
            StartTime = $inst.StartTime
            Uptime    = $uptime
            SessionId = $inst.SharedState.SessionId
            Url       = "http://localhost:$($inst.Port)"
        }
    }
    return $result
}
