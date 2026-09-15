import CoreGraphics
import CoreMedia
import CoreVideo
import Foundation
import ScreenCaptureKit

private let headerSize = 16

private struct Arguments {
    let output: String
    let width: Int
    let height: Int
    let fps: Int32
    let excludedPID: pid_t?
    let windowTitle: String?

    init?() {
        let values = CommandLine.arguments
        func value(after flag: String) -> String? {
            guard let index = values.firstIndex(of: flag), index + 1 < values.count else { return nil }
            return values[index + 1]
        }

        guard let output = value(after: "--output"),
              let widthText = value(after: "--width"), let width = Int(widthText), width > 0,
              let heightText = value(after: "--height"), let height = Int(heightText), height > 0,
              let fpsText = value(after: "--fps"), let fps = Int32(fpsText), fps > 0 else {
            return nil
        }
        self.output = output
        self.width = width
        self.height = height
        self.fps = fps
        self.excludedPID = value(after: "--exclude-pid").flatMap { pid_t($0) }
        self.windowTitle = value(after: "--window-title")
    }
}

private final class SharedFrameWriter: NSObject, SCStreamOutput {
    private let width: Int
    private let height: Int
    private let frameBytes: Int
    private let mapping: UnsafeMutableRawPointer
    private var sequence: UInt64 = 0

    init(output: String, width: Int, height: Int) throws {
        self.width = width
        self.height = height
        self.frameBytes = width * height * 4

        let parent = URL(fileURLWithPath: output).deletingLastPathComponent().path
        try FileManager.default.createDirectory(atPath: parent, withIntermediateDirectories: true)

        let descriptor = open(output, O_RDWR | O_CREAT | O_TRUNC, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                          userInfo: [NSLocalizedDescriptionKey: "Unable to create \(output)"])
        }
        let totalBytes = headerSize + frameBytes
        guard ftruncate(descriptor, off_t(totalBytes)) == 0 else {
            let savedErrno = errno
            close(descriptor)
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(savedErrno),
                          userInfo: [NSLocalizedDescriptionKey: "Unable to size \(output)"])
        }
        let address = mmap(nil, totalBytes, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0)
        close(descriptor)
        guard address != MAP_FAILED, let address else {
            throw NSError(domain: NSPOSIXErrorDomain, code: Int(errno),
                          userInfo: [NSLocalizedDescriptionKey: "Unable to map \(output)"])
        }
        self.mapping = address
        super.init()
        writeHeader(sequence: 0)
    }

    deinit {
        munmap(mapping, headerSize + frameBytes)
    }

    private func writeHeader(sequence: UInt64) {
        mapping.storeBytes(of: sequence.littleEndian, toByteOffset: 0, as: UInt64.self)
        mapping.storeBytes(of: UInt32(width).littleEndian, toByteOffset: 8, as: UInt32.self)
        mapping.storeBytes(of: UInt32(height).littleEndian, toByteOffset: 12, as: UInt32.self)
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of outputType: SCStreamOutputType) {
        guard outputType == .screen,
              sampleBuffer.isValid,
              let imageBuffer = sampleBuffer.imageBuffer,
              CVPixelBufferGetWidth(imageBuffer) == width,
              CVPixelBufferGetHeight(imageBuffer) == height else { return }

        CVPixelBufferLockBaseAddress(imageBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(imageBuffer, .readOnly) }
        guard let source = CVPixelBufferGetBaseAddress(imageBuffer) else { return }

        sequence &+= 2
        writeHeader(sequence: sequence - 1)

        let sourceStride = CVPixelBufferGetBytesPerRow(imageBuffer)
        let destinationStride = width * 4
        let destination = mapping.advanced(by: headerSize)
        if sourceStride == destinationStride {
            memcpy(destination, source, frameBytes)
        } else {
            for row in 0..<height {
                memcpy(destination.advanced(by: row * destinationStride),
                       source.advanced(by: row * sourceStride), destinationStride)
            }
        }

        OSMemoryBarrier()
        writeHeader(sequence: sequence)
    }
}

private func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data(("macos_capture_bridge: \(message)\n").utf8))
    exit(code)
}

private final class CaptureDelegate: NSObject, SCStreamDelegate {
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fail("screen capture stopped: \(error.localizedDescription)", code: 79)
    }
}

guard let arguments = Arguments() else {
    fail("usage: macos_capture_bridge --output PATH --width N --height N --fps N [--window-title TITLE]", code: 2)
}

guard CGPreflightScreenCaptureAccess() || CGRequestScreenCaptureAccess() else {
    fail("Screen Recording permission is required. Enable it for EDAPGui (or the launching terminal) in System Settings > Privacy & Security > Screen & System Audio Recording, then relaunch EDAPGui.", code: 77)
}

let contentSemaphore = DispatchSemaphore(value: 0)
var availableContent: SCShareableContent?
var contentError: Error?
SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { content, error in
    availableContent = content
    contentError = error
    contentSemaphore.signal()
}
contentSemaphore.wait()

if let contentError {
    fail("could not enumerate displays: \(contentError.localizedDescription)")
}
guard let displays = availableContent?.displays, !displays.isEmpty else {
    fail("no capturable displays were returned by ScreenCaptureKit")
}

let mainDisplayID = CGMainDisplayID()
guard let display = displays.first(where: { $0.displayID == mainDisplayID }) ?? displays.first else {
    fail("could not select a display")
}

private let writer: SharedFrameWriter
do {
    writer = try SharedFrameWriter(
        output: arguments.output, width: arguments.width, height: arguments.height)
} catch {
    fail(error.localizedDescription)
}

let configuration = SCStreamConfiguration()
configuration.width = arguments.width
configuration.height = arguments.height
configuration.pixelFormat = kCVPixelFormatType_32BGRA
configuration.minimumFrameInterval = CMTime(value: 1, timescale: arguments.fps)
configuration.queueDepth = 3
configuration.showsCursor = false
configuration.capturesAudio = false

let filter: SCContentFilter
var eliteProcessMonitor: DispatchSourceProcess?
var eliteWindowMonitor: DispatchSourceTimer?
if let wantedTitle = arguments.windowTitle {
    let needle = wantedTitle.lowercased()
    guard let window = availableContent?.windows.first(where: {
        ($0.title ?? "").lowercased().contains(needle)
    }) else {
        fail("could not find a capturable Elite window named '\(wantedTitle)'")
    }
    guard let application = window.owningApplication else {
        fail("could not resolve the Wine application that owns '\(wantedTitle)'")
    }
    let windowDisplay = displays.first(where: { $0.frame.intersects(window.frame) }) ?? display
    let sourceRect = CGRect(
        x: window.frame.origin.x - windowDisplay.frame.origin.x,
        y: window.frame.origin.y - windowDisplay.frame.origin.y,
        width: window.frame.width,
        height: window.frame.height)
    configuration.sourceRect = sourceRect
    FileHandle.standardError.write(Data((
        "macos_capture_bridge: capturing '\(window.title ?? wantedTitle)' " +
        "owned by \(application.applicationName)\n").utf8))
    // Wine can replace its native SCWindow while Elite continues running.
    // Filtering the owning application keeps the stream valid across that
    // replacement; sourceRect limits the output to Elite's current frame.
    filter = SCContentFilter(
        display: windowDisplay, including: [application], exceptingWindows: [])
    let processMonitor = DispatchSource.makeProcessSource(
        identifier: application.processID, eventMask: .exit, queue: .main)
    processMonitor.setEventHandler {
        fail("Elite process exited; stopping EDAPGui.", code: 78)
    }
    processMonitor.resume()
    eliteProcessMonitor = processMonitor

    // Wine can outlive Elite, in which case process monitoring alone would
    // leave EDAP consuming blank frames. Allow brief window replacement, then
    // terminate the owned runtime when Elite is genuinely gone.
    var missedWindowChecks = 0
    let windowMonitor = DispatchSource.makeTimerSource(queue: .main)
    windowMonitor.schedule(deadline: .now() + 1, repeating: 0.5)
    windowMonitor.setEventHandler {
        // Use all windows here so switching Spaces or minimizing Elite does not
        // look like an application exit. The capture path itself still only
        // starts from a visible, shareable Elite window.
        let windows = CGWindowListCopyWindowInfo(
            [.optionAll, .excludeDesktopElements], kCGNullWindowID)
            as? [[String: Any]] ?? []
        let found = windows.contains { item in
            let title = item[kCGWindowName as String] as? String ?? ""
            let pid = (item[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value
            return pid == application.processID && title.lowercased().contains(needle)
        }
        missedWindowChecks = found ? 0 : missedWindowChecks + 1
        if missedWindowChecks >= 4 {
            fail("Elite window closed; stopping EDAPGui.", code: 78)
        }
    }
    windowMonitor.resume()
    eliteWindowMonitor = windowMonitor
} else {
    let excludedApplications: [SCRunningApplication]
    if let excludedPID = arguments.excludedPID,
       let application = availableContent?.applications.first(where: { $0.processID == excludedPID }) {
        excludedApplications = [application]
    } else {
        excludedApplications = []
    }
    filter = SCContentFilter(
        display: display, excludingApplications: excludedApplications, exceptingWindows: [])
}
private let streamDelegate = CaptureDelegate()
let stream = SCStream(filter: filter, configuration: configuration, delegate: streamDelegate)
let captureQueue = DispatchQueue(label: "com.moltenvr.edap.capture", qos: .userInteractive)
do {
    try stream.addStreamOutput(writer, type: .screen, sampleHandlerQueue: captureQueue)
} catch {
    fail("could not attach the capture output: \(error.localizedDescription)")
}

let startSemaphore = DispatchSemaphore(value: 0)
var startError: Error?
stream.startCapture { error in
    startError = error
    startSemaphore.signal()
}
startSemaphore.wait()
if let startError {
    fail("could not start screen capture: \(startError.localizedDescription)")
}

dispatchMain()
