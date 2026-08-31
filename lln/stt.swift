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

func startRecognitionTask() {
    let req = SFSpeechAudioBufferRecognitionRequest()
    req.shouldReportPartialResults = true
    request = req

    task = recognizer.recognitionTask(with: req) { result, error in
        if let result = result {
            let text = result.bestTranscription.formattedString
            if result.isFinal {
                printJSON(["type": "final", "text": text])
                restartRecognitionTask()
            } else {
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

func restartRecognitionTask() {
    task?.cancel()
    task = nil
    request?.endAudio()
    request = nil
    startRecognitionTask()
}

func setupAudioAndStart() {
    let inputNode = audioEngine.inputNode
    let format = inputNode.inputFormat(forBus: 0)

    inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
        request?.append(buffer)
    }

    do {
        try audioEngine.start()
    } catch {
        printJSON(["type": "error", "message": "engine_start_failed: \(error.localizedDescription)"])
        exit(5)
    }

    startRecognitionTask()
    printJSON(["type": "state", "status": "listening"])
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
