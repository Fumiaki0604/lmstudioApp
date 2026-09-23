// マイク入力を常時認識し、部分/確定結果をJSON行としてstdoutに流す。
// projectNoah の SpeechBridge.swift (SFSpeechRecognizer + AVAudioEngine) を
// Tauri/Rustを介さない単体CLIとして書き直したもの。
//
// ビルド:
//   ./build_stt.sh
// 実行:
//   open stt.app
//
// 出力(1行1JSON):
//   {"type":"state","status":"listening"}
//   {"type":"partial","text":"..."}
//   {"type":"final","text":"..."}
//   {"type":"error","message":"..."}

import AVFoundation
import Foundation
import Speech

func printJSON(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict),
        let str = String(data: data, encoding: .utf8)
    {
        print(str)
        fflush(stdout)
    }
}

guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "ja-JP")) else {
    printJSON(["type": "error", "message": "recognizer_init_failed"])
    exit(1)
}

let audioEngine = AVAudioEngine()
var request: SFSpeechAudioBufferRecognitionRequest?
var task: SFSpeechRecognitionTask?
var isReconfiguring = false
var silenceTimer: Timer?
let silenceInterval: TimeInterval = 1.2  // この秒数だけ新しい発話が無ければ確定させる

var levelSum: Double = 0
var levelCount: Int = 0

func resetLevel() {
    levelSum = 0
    levelCount = 0
}

func averageLevel() -> Double {
    levelCount > 0 ? levelSum / Double(levelCount) : 0
}

func resetSilenceTimer() {
    silenceTimer?.invalidate()
    silenceTimer = Timer.scheduledTimer(withTimeInterval: silenceInterval, repeats: false) { _ in
        request?.endAudio()
    }
}

func startRecognitionTask() {
    let req = SFSpeechAudioBufferRecognitionRequest()
    req.shouldReportPartialResults = true
    request = req
    resetLevel()

    task = recognizer.recognitionTask(with: req) { result, error in
        if let result = result {
            let text = result.bestTranscription.formattedString
            if result.isFinal {
                silenceTimer?.invalidate()
                printJSON(["type": "final", "text": text, "level": averageLevel()])
                restartRecognitionTask()
            } else {
                resetSilenceTimer()
                printJSON(["type": "partial", "text": text])
            }
        }
        if let error = error {
            let desc = error.localizedDescription
            if desc.contains("cancel") || desc.contains("Cancel") {
                // 自分でキャンセルした時の想定内エラーなので無視
            } else {
                printJSON(["type": "error", "message": desc])
                restartRecognitionTask()
            }
        }
    }
}

var lastRestartTime: Date = .distantPast

func restartRecognitionTask() {
    task?.cancel()
    task = nil
    request?.endAudio()
    request = nil

    // 何らかの理由で再起動が高頻度に連発する場合、無限ループで
    // ログを吐き続けるのを防ぐため、最低間隔を空ける。
    let elapsed = Date().timeIntervalSince(lastRestartTime)
    let minInterval = 0.3
    lastRestartTime = Date()
    if elapsed < minInterval {
        DispatchQueue.main.asyncAfter(deadline: .now() + (minInterval - elapsed)) {
            startRecognitionTask()
        }
    } else {
        startRecognitionTask()
    }
}

func installTap() {
    let inputNode = audioEngine.inputNode
    let format = inputNode.inputFormat(forBus: 0)

    inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
        request?.append(buffer)

        if let channelData = buffer.floatChannelData?[0] {
            let frameLength = Int(buffer.frameLength)
            var sum: Float = 0
            for i in 0..<frameLength {
                sum += channelData[i] * channelData[i]
            }
            let rms = frameLength > 0 ? sqrt(sum / Float(frameLength)) : 0
            levelSum += Double(rms)
            levelCount += 1
        }
    }
}

func setupAudioAndStart() {
    installTap()

    do {
        try audioEngine.start()
    } catch {
        printJSON(["type": "error", "message": "engine_start_failed: \(error.localizedDescription)"])
        exit(5)
    }

    startRecognitionTask()
    printJSON(["type": "state", "status": "listening"])

    // ヘッドフォン接続など、入力デバイスが変わった時に自動で追従する。
    // AVAudioEngineはデフォルトでは変化に追従しないため、これが無いと
    // 見た目上は動いていても実際は音を拾えなくなる。
    //
    // 注意: engine.stop()/start()自体がこの通知を再度発生させることが
    // あり、ガード無しだと無限ループになる(実際に640万行のエラーログを
    // 吐き続けるまでハングした)。isReconfiguring フラグ+一定時間の
    // クールダウンで再入・連鎖を防ぐ。
    NotificationCenter.default.addObserver(
        forName: .AVAudioEngineConfigurationChange, object: audioEngine, queue: nil
    ) { _ in
        DispatchQueue.main.async {
            guard !isReconfiguring else { return }
            isReconfiguring = true

            printJSON(["type": "state", "status": "reconfiguring"])
            audioEngine.inputNode.removeTap(onBus: 0)
            audioEngine.stop()
            installTap()
            do {
                try audioEngine.start()
                restartRecognitionTask()
                printJSON(["type": "state", "status": "listening"])
            } catch {
                printJSON(["type": "error", "message": "reconfigure_failed: \(error.localizedDescription)"])
            }

            // stop()/start()が引き起こす後続の通知が収まるまで、
            // 少し待ってから再入を解禁する。
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) {
                isReconfiguring = false
            }
        }
    }
}

func requestPermissionsAndStart() {
    SFSpeechRecognizer.requestAuthorization { speechStatus in
        let speechOk = (speechStatus == .authorized)
        AVCaptureDevice.requestAccess(for: .audio) { micGranted in
            DispatchQueue.main.async {
                guard speechOk else {
                    printJSON(["type": "error", "message": "speech_denied"])
                    exit(2)
                }
                guard micGranted else {
                    printJSON(["type": "error", "message": "mic_denied"])
                    exit(3)
                }
                guard recognizer.isAvailable else {
                    printJSON(["type": "error", "message": "recognizer_unavailable"])
                    exit(4)
                }
                setupAudioAndStart()
            }
        }
    }
}

DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
    requestPermissionsAndStart()
}
RunLoop.main.run()
