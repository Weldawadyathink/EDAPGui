import ApplicationServices
import CoreGraphics
import Foundation

private struct Binding {
    let id: String
    let keyCode: CGKeyCode
    let flags: CGEventFlags
}

private let keyCodes: [String: CGKeyCode] = [
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
    "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
    "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22,
    "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29,
    "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
    "n": 45, "m": 46, "space": 49, "escape": 53, "insert": 114,
    "home": 115, "page_up": 116, "end": 119, "page_down": 121,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "tab": 48, "enter": 36, "backspace": 51, "delete": 117, "left": 123, "right": 124, "down": 125, "up": 126
]

private func normalized(_ value: String) -> String {
    let aliases = [
        "control": "ctrl", "option": "alt", "command": "cmd",
        "pgup": "page_up", "pageup": "page_up", "page up": "page_up",
        "pgdn": "page_down", "pgdown": "page_down", "pagedown": "page_down",
        "page down": "page_down"
    ]
    let lowered = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    return aliases[lowered] ?? lowered
}

private func parseBinding(_ raw: String) -> Binding? {
    guard let equals = raw.firstIndex(of: "=") else { return nil }
    let id = String(raw[..<equals])
    let chord = String(raw[raw.index(after: equals)...])
    var flags: CGEventFlags = []
    var base: String?
    for rawPart in chord.replacingOccurrences(of: "-", with: "+").split(separator: "+") {
        let part = normalized(String(rawPart))
        switch part {
        case "ctrl": flags.insert(.maskControl)
        case "shift": flags.insert(.maskShift)
        case "alt": flags.insert(.maskAlternate)
        case "cmd": flags.insert(.maskCommand)
        default: base = part
        }
    }
    guard let base, let keyCode = keyCodes[base] else { return nil }
    return Binding(id: id, keyCode: keyCode, flags: flags)
}

private var bindings: [Binding] = []
private var invalidBindings: [String] = []
var index = 1
while index < CommandLine.arguments.count {
    if CommandLine.arguments[index] == "--binding", index + 1 < CommandLine.arguments.count {
        let raw = CommandLine.arguments[index + 1]
        if let binding = parseBinding(raw) {
            bindings.append(binding)
        } else {
            invalidBindings.append(raw)
        }
        index += 2
    } else {
        invalidBindings.append(CommandLine.arguments[index])
        index += 1
    }
}

guard invalidBindings.isEmpty else {
    FileHandle.standardError.write(Data(
        "macos_hotkey_bridge: invalid bindings: \(invalidBindings.joined(separator: ", "))\n".utf8))
    exit(2)
}
guard !bindings.isEmpty else {
    FileHandle.standardError.write(Data("macos_hotkey_bridge: no valid bindings\n".utf8))
    exit(2)
}

private var activeTap: CFMachPort?
let callback: CGEventTapCallBack = { _, type, event, _ in
    if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
        if let tap = activeTap { CGEvent.tapEnable(tap: tap, enable: true) }
        return Unmanaged.passUnretained(event)
    }
    guard type == .keyDown,
          event.getIntegerValueField(.keyboardEventAutorepeat) == 0 else {
        return Unmanaged.passUnretained(event)
    }
    let keyCode = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
    let modifierMask: CGEventFlags = [.maskControl, .maskShift, .maskAlternate, .maskCommand]
    let eventFlags = event.flags.intersection(modifierMask)
    for binding in bindings where binding.keyCode == keyCode && eventFlags == binding.flags {
        let data = try! JSONSerialization.data(withJSONObject: ["id": binding.id])
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0a]))
    }
    return Unmanaged.passUnretained(event)
}

let mask = CGEventMask(1 << CGEventType.keyDown.rawValue)
guard let tap = CGEvent.tapCreate(
    tap: .cgSessionEventTap, place: .headInsertEventTap, options: .listenOnly,
    eventsOfInterest: mask, callback: callback, userInfo: nil) else {
    FileHandle.standardError.write(Data(
        "macos_hotkey_bridge: Input Monitoring permission is required\n".utf8))
    exit(77)
}
activeTap = tap
let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
CFRunLoopAddSource(CFRunLoopGetCurrent(), source, .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)

// stdin is a lifetime pipe owned by Python. EOF means the parent exited—even
// after a force kill—so this helper cannot linger as an orphan process.
FileHandle.standardInput.readabilityHandler = { handle in
    if handle.availableData.isEmpty {
        CFRunLoopStop(CFRunLoopGetMain())
    }
}
let ready = try! JSONSerialization.data(withJSONObject: [
    "ready": true, "bindings": bindings.count
])
FileHandle.standardOutput.write(ready)
FileHandle.standardOutput.write(Data([0x0a]))
CFRunLoopRun()
