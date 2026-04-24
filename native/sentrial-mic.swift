// sentrial-mic — native microphone capture helper.
//
// Why this exists: Python.app's Info.plist has no NSMicrophoneUsageDescription,
// so TCC silently denies mic access for any Python-based CoreAudio call. Apple
// attributes mic to the binary that makes the CoreAudio call, not the parent
// bundle, so wrapping Python in a Sentrial.app shim doesn't help.
//
// This Swift binary lives inside Sentrial.app/Contents/MacOS/, so TCC sees it
// as part of Sentrial.app and honors Sentrial's Info.plist mic usage key. It
// captures the default input device, resamples to 16 kHz mono PCM int16, and
// writes raw samples to stdout. Python reads from the subprocess pipe and
// forwards to Deepgram.
//
// Exit conditions:
//   - stdin closes (parent died)       → exit 0
//   - AVAudioEngine start fails        → exit 4
//   - mic permission denied            → exit 2 (after explicit request)

import AVFoundation
import Foundation

let stderr = FileHandle.standardError
func logErr(_ s: String) { stderr.write((s + "\n").data(using: .utf8)!) }

// Diagnostics: what does macOS think this process is? Helps debug whether the
// embedded Info.plist / bundle attribution is actually being picked up.
let bundle = Bundle.main
logErr("sentrial-mic: bundleID=\(bundle.bundleIdentifier ?? "(nil)") "
     + "execPath=\(bundle.executablePath ?? "(nil)")")
let micKey = bundle.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") as? String
logErr("sentrial-mic: NSMicrophoneUsageDescription=\(micKey.map { "\"\($0)\"" } ?? "MISSING")")

// -- 1. Explicitly request mic permission (surfaces TCC prompt first time). --
let status = AVCaptureDevice.authorizationStatus(for: .audio)
logErr("sentrial-mic: initial authorizationStatus=\(status.rawValue) "
     + "(0=ND 1=Restricted 2=Denied 3=Authorized)")
if status == .notDetermined {
    let sema = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { g in
        granted = g
        sema.signal()
    }
    sema.wait()
    if !granted {
        logErr("sentrial-mic: microphone permission denied")
        exit(2)
    }
} else if status == .denied || status == .restricted {
    logErr("sentrial-mic: microphone access is \(status == .denied ? "denied" : "restricted")")
    exit(2)
}

// -- 2. Engine + tap on default input at native format. Convert to target. --
let engine = AVAudioEngine()
let input = engine.inputNode
let inFormat = input.inputFormat(forBus: 0)

guard let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                    sampleRate: 16000,
                                    channels: 1,
                                    interleaved: true) else {
    logErr("sentrial-mic: failed to construct output format")
    exit(3)
}

guard let converter = AVAudioConverter(from: inFormat, to: outFormat) else {
    logErr("sentrial-mic: failed to construct converter \(inFormat) -> \(outFormat)")
    exit(3)
}

let stdout = FileHandle.standardOutput

input.installTap(onBus: 0, bufferSize: 1024, format: inFormat) { buffer, _ in
    // Output frame capacity: input frames × sample-rate ratio, rounded up.
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
    // write() may short-write on pipe buffer full; Data handles this internally
    // for FileHandle by looping until all bytes are written. If the reader died,
    // SIGPIPE will kill us — that's how we detect parent exit beyond stdin close.
    do {
        try stdout.write(contentsOf: data)
    } catch {
        exit(0)
    }
}

do {
    try engine.start()
} catch {
    logErr("sentrial-mic: engine.start failed: \(error)")
    exit(4)
}

// Watch stdin for close — when Python parent dies, stdin EOFs.
DispatchQueue.global(qos: .utility).async {
    let handle = FileHandle.standardInput
    while true {
        let data = handle.availableData
        if data.isEmpty {
            // Stdin closed — shutdown.
            engine.stop()
            exit(0)
        }
    }
}

// Run indefinitely.
RunLoop.main.run()
