// セミナー録音用のヘルパー。マイク(会場の生音声)か、Macの内部音声(ScreenCaptureKit、
// YouTube/Zoom等)を16kHz・モノラル・16bitの生PCMで録音する。
//
// stt.appとは別プロセスにしている。stt.appはエラー連発時にassistant.pyから
// 再起動されるため、同じプロセスで録音すると録音まで途切れてしまう。
// 異常終了しても録音が失われないよう、SEGMENT_SEC秒ごとにファイルを分け、
// ヘッダの無い生PCM(seg_000.pcm, seg_001.pcm, ...)で書き出す(WAV/m4aは
// 正常終了時にヘッダを書き直す必要があり、途中で落ちると読めなくなるため)。
//
// 使い方(seminar.pyから open -n rec.app --stdout ... --args で起動される):
//   rec --mode mic|system --out <dir> [--max-sec 10800] [--segment-sec 300]
// SIGTERMで現在のファイルを閉じて正常終了する。--max-secに達した場合も同様。
import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

func log(_ msg: String) {
    print("[rec] \(msg)")
    fflush(stdout)
}

func arg(_ name: String, _ fallback: String? = nil) -> String? {
    let args = CommandLine.arguments
    if let i = args.firstIndex(of: name), i + 1 < args.count { return args[i + 1] }
    return fallback
}

let mode = arg("--mode", "mic")!
let outDir = arg("--out") ?? ""
if outDir.isEmpty {
    log("--out is required")
    exit(2)
}
let maxSec = Double(arg("--max-sec", "10800")!) ?? 10800
let segmentSec = Double(arg("--segment-sec", "300")!) ?? 300

let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: true)!
let framesPerSegment = AVAudioFramePosition(16000 * segmentSec)

// ---------------------------------------------------------------------------
// 書き出し(オーディオのコールバックスレッドでディスクに触らないよう、専用キューで行う)
// ---------------------------------------------------------------------------

let writeQueue = DispatchQueue(label: "rec.write")
var segmentIndex = 0
var framesInSegment: AVAudioFramePosition = 0
var handle: FileHandle?

func openSegment() {
    handle?.closeFile()
    let path = (outDir as NSString).appendingPathComponent(String(format: "seg_%03d.pcm", segmentIndex))
    FileManager.default.createFile(atPath: path, contents: nil)
    handle = FileHandle(forWritingAtPath: path)
    framesInSegment = 0
    log("segment \(segmentIndex)")
}

func write(_ buffer: AVAudioPCMBuffer) {
    guard let ptr = buffer.int16ChannelData?[0] else { return }
    let data = Data(bytes: ptr, count: Int(buffer.frameLength) * 2)
    writeQueue.async {
        if handle == nil || framesInSegment >= framesPerSegment {
            if handle != nil { segmentIndex += 1 }
            openSegment()
        }
        handle?.write(data)
        framesInSegment += AVAudioFramePosition(buffer.frameLength)
    }
}

// ---------------------------------------------------------------------------
// 入力フォーマット → 16kHz/mono/Int16 への変換(マイク・内部音声で共通)
// ---------------------------------------------------------------------------

var converter: AVAudioConverter?

func convertAndWrite(_ input: AVAudioPCMBuffer) {
    if converter == nil || converter!.inputFormat != input.format {
        converter = AVAudioConverter(from: input.format, to: outFormat)
    }
    guard let converter = converter else { return }
    let ratio = outFormat.sampleRate / input.format.sampleRate
    let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 32
    guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }
    var consumed = false
    var error: NSError?
    converter.convert(to: out, error: &error) { _, status in
        if consumed {
            status.pointee = .noDataNow
            return nil
        }
        consumed = true
        status.pointee = .haveData
        return input
    }
    if let error = error {
        log("convert error: \(error)")
        return
    }
    if out.frameLength > 0 { write(out) }
}

// ---------------------------------------------------------------------------
// マイク
// ---------------------------------------------------------------------------

let audioEngine = AVAudioEngine()

func startMic() {
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        guard granted else {
            log("microphone permission denied")
            exit(3)
        }
        DispatchQueue.main.async {
            let input = audioEngine.inputNode
            input.installTap(onBus: 0, bufferSize: 4096, format: input.inputFormat(forBus: 0)) { buffer, _ in
                convertAndWrite(buffer)
            }
            do {
                try audioEngine.start()
                log("recording started (mic)")
            } catch {
                log("engine start failed: \(error)")
                exit(4)
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Macの内部音声(ScreenCaptureKit)
// ---------------------------------------------------------------------------

final class SystemAudioOutput: NSObject, SCStreamOutput, SCStreamDelegate {
    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sb.isValid,
              let fd = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fd),
              let format = AVAudioFormat(streamDescription: asbd)
        else { return }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
        guard frames > 0, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        pcm.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sb, at: 0, frameCount: Int32(frames), into: pcm.mutableAudioBufferList
        )
        if status == noErr { convertAndWrite(pcm) }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("system audio stream stopped: \(error)")
        finish(code: 5)
    }
}

let systemOutput = SystemAudioOutput()
var scStream: SCStream?

func startSystemAudio() {
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
        guard let display = content?.displays.first else {
            // 「画面収録とシステムオーディオ録音」の許可が無いとここに来る
            log("screen capture unavailable (permission?): \(String(describing: error))")
            exit(3)
        }
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = 16000
        config.channelCount = 1
        config.excludesCurrentProcessAudio = true
        // 映像は使わないが、ScreenCaptureKitはディスプレイ指定が必須なので最小限にする
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(filter: filter, configuration: config, delegate: systemOutput)
        do {
            try stream.addStreamOutput(systemOutput, type: .audio, sampleHandlerQueue: DispatchQueue(label: "rec.sc"))
        } catch {
            log("addStreamOutput failed: \(error)")
            exit(4)
        }
        stream.startCapture { error in
            if let error = error {
                log("startCapture failed: \(error)")
                exit(4)
            }
            log("recording started (system)")
        }
        scStream = stream
    }
}

// ---------------------------------------------------------------------------
// 終了処理
// ---------------------------------------------------------------------------

var finishing = false

func finish(code: Int32 = 0) {
    DispatchQueue.main.async {
        if finishing { return }
        finishing = true
        audioEngine.stop()
        scStream?.stopCapture { _ in }
        writeQueue.sync {
            handle?.synchronizeFile()
            handle?.closeFile()
        }
        log("finished")
        exit(code)
    }
}

var signalSources: [DispatchSourceSignal] = []  // 解放されるとハンドラが止まるので保持する
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    source.setEventHandler { finish() }
    source.resume()
    signalSources.append(source)
}

try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
try? String(getpid()).write(
    toFile: (outDir as NSString).appendingPathComponent("rec.pid"), atomically: true, encoding: .utf8
)

DispatchQueue.main.asyncAfter(deadline: .now() + maxSec) {
    log("max duration reached")
    finish()
}

if mode == "system" {
    startSystemAudio()
} else {
    startMic()
}
dispatchMain()
