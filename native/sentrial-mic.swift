// sentrial-mic — native microphone capture helper.
//
// Why this exists: Python.app's Info.plist has no NSMicrophoneUsageDescription,
// so TCC silently denies mic access for any Python-based CoreAudio call. Apple
// attributes mic to the binary that makes the CoreAudio call, not the parent
// bundle, so wrapping Python in a Sentrial.app shim doesn't help.
//
// This Swift binary has a real Info.plist embedded in its Mach-O
// __TEXT,__info_plist section, carrying a CFBundleIdentifier and
// NSMicrophoneUsageDescription. macOS reads those when TCC decides whether
// to prompt. The binary captures the default input device, resamples to
// 16 kHz mono int16, and writes raw samples to stdout. Python reads from
// the subprocess pipe and forwards to Deepgram.
//
// The runloop semantics are important: requestAccess(for:.audio) needs the
// main runloop to be alive to render the permission dialog. We kick off the
// request, return control to the runloop, and start capture only from its
// completion handler.

import AVFoundation
import Foundation

let stderr = FileHandle.standardError
let stdout = FileHandle.standardOutput
func logErr(_ s: String) {
    if let d = (s + "\n").data(using: .utf8) { stderr.write(d) }
}

// Diagnostics so we can see exactly what macOS thinks the process is.
let mainBundle = Bundle.main
logErr("sentrial-mic: bundleID=\(mainBundle.bundleIdentifier ?? "(nil)") "
     + "execPath=\(mainBundle.executablePath ?? "(nil)")")
let micKey = mainBundle.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String
logErr("sentrial-mic: NSMicrophoneUsageDescription=\(micKey.map { "\"\($0)\"" } ?? "MISSING")")

// Engine is module-level so the stdin-watcher can stop it on parent exit.
let engine = AVAudioEngine()

func startCapture() {
    let input = engine.inputNode
    let inFormat = input.inputFormat(forBus: 0)
    logErr("sentrial-mic: inputFormat=\(inFormat)")

    guard let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                        sampleRate: 16000,
                                        channels: 1,
                                        interleaved: true) else {
        logErr("sentrial-mic: failed to construct output format")
        exit(3)
    }
    guard let converter = AVAudioConverter(from: inFormat, to: outFormat) else {
        logErr("sentrial-mic: failed to construct converter")
        exit(3)
    }

    input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { buffer, _ in
        let ratio = outFormat.sampleRate / inFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 16)
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: outCapacity) else { return }

        var supplied = false
        var convErr: NSError?
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if supplied {
                outStatus.pointee = .noDataNow
                return nil
            }
            supplied = true
            outStatus.pointee = .haveData
            return buffer
        }
        let result = converter.convert(to: outBuf, error: &convErr, withInputFrom: inputBlock)
        if result == .error || convErr != nil { return }

        guard let ptr = outBuf.int16ChannelData?[0] else { return }
        let byteCount = Int(outBuf.frameLength) * MemoryLayout<Int16>.size
        if byteCount <= 0 { return }
        let data = Data(bytes: ptr, count: byteCount)
        do {
            try stdout.write(contentsOf: data)
        } catch {
            exit(0)
        }
    }

    do {
        try engine.start()
        logErr("sentrial-mic: engine started")
    } catch {
        logErr("sentrial-mic: engine.start failed: \(error)")
        exit(4)
    }
}

// -- Permission request runs on the main runloop. The completion handler,
// -- which may fire after a user-facing prompt, drives the next step.
let status = AVCaptureDevice.authorizationStatus(for: .audio)
logErr("sentrial-mic: initial authorizationStatus=\(status.rawValue) "
     + "(0=ND 1=Restricted 2=Denied 3=Authorized)")

switch status {
case .authorized:
    startCapture()
case .denied, .restricted:
    logErr("sentrial-mic: microphone access is \(status == .denied ? "denied" : "restricted")")
    exit(2)
case .notDetermined:
    logErr("sentrial-mic: requesting mic access — a system prompt should appear")
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        logErr("sentrial-mic: requestAccess completion granted=\(granted)")
        if granted {
            // Hop to main for engine start — AVAudioEngine wants a sane queue.
            DispatchQueue.main.async { startCapture() }
        } else {
            logErr("sentrial-mic: microphone permission denied")
            exit(2)
        }
    }
@unknown default:
    logErr("sentrial-mic: unknown authorization status \(status.rawValue)")
    exit(2)
}

// Stdin-watcher: when Python closes stdin (or dies), shut down cleanly.
DispatchQueue.global(qos: .utility).async {
    let handle = FileHandle.standardInput
    while true {
        let data = handle.availableData
        if data.isEmpty {
            engine.stop()
            exit(0)
        }
    }
}

// Keep the process alive so the AVCaptureDevice.requestAccess prompt can be
// presented and its completion handler can run. Without this, the binary
// would exit before macOS shows a prompt — which is exactly the "denied
// without a dialog" symptom we were hitting.
RunLoop.main.run()
