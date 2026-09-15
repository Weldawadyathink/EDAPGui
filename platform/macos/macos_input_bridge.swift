import AppKit
import ApplicationServices
import CoreGraphics
import Foundation

private let arguments = CommandLine.arguments
private func value(after flag: String) -> String? {
    guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}
private let wantedTitle = value(after: "--window-title") ?? "Elite - Dangerous (CLIENT)"

private struct EliteWindow {
    let pid: pid_t
    let bounds: CGRect
    let owner: String
    let title: String
}

private func eliteWindow(waitingUpTo timeout: TimeInterval = 0.75) -> EliteWindow? {
    let needle = wantedTitle.lowercased()
    let deadline = Date().addingTimeInterval(timeout)
    repeat {
        let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        if let raw = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] {
            for item in raw {
                let title = item[kCGWindowName as String] as? String ?? ""
                guard title.lowercased().contains(needle) else { continue }
                guard let pidNumber = item[kCGWindowOwnerPID as String] as? NSNumber,
                      let boundsValue = item[kCGWindowBounds as String],
                      let bounds = CGRect(dictionaryRepresentation: boundsValue as! CFDictionary) else { continue }
                return EliteWindow(
                    pid: pidNumber.int32Value,
                    bounds: bounds,
                    owner: item[kCGWindowOwnerName as String] as? String ?? "",
                    title: title)
            }
        }
        if Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
    } while Date() < deadline
    return nil
}

// DirectInput/Set-1 scan codes to macOS virtual hardware key codes. EDAP reads
// these PC scan codes from Elite's .binds file; controls therefore remain driven
// by the user's actual bindings rather than a second macOS key configuration.
private let macKeyCode: [Int: CGKeyCode] = [
    1: 53, 2: 18, 3: 19, 4: 20, 5: 21, 6: 23, 7: 22, 8: 26, 9: 28, 10: 25,
    11: 29, 12: 27, 13: 24, 14: 51, 15: 48, 16: 12, 17: 13, 18: 14, 19: 15,
    20: 17, 21: 16, 22: 32, 23: 34, 24: 31, 25: 35, 26: 33, 27: 30, 28: 36,
    29: 59, 30: 0, 31: 1, 32: 2, 33: 3, 34: 5, 35: 4, 36: 38, 37: 40,
    38: 37, 39: 41, 40: 39, 41: 50, 42: 56, 43: 42, 44: 6, 45: 7, 46: 8,
    47: 9, 48: 11, 49: 45, 50: 46, 51: 43, 52: 47, 53: 44, 54: 60, 55: 67,
    56: 58, 57: 49, 58: 57, 59: 122, 60: 120, 61: 99, 62: 118, 63: 96,
    64: 97, 65: 98, 66: 100, 67: 101, 68: 109, 69: 71, 71: 89, 72: 91, 73: 92,
    74: 78, 75: 86, 76: 87, 77: 88, 78: 69, 79: 83, 80: 84, 81: 85, 82: 82,
    83: 65, 87: 103, 88: 111, 156: 76, 157: 62, 181: 75, 184: 61, 199: 115,
    200: 126, 201: 116, 203: 123, 205: 124, 207: 119, 208: 125, 209: 121,
    210: 114, 211: 117
]

private var heldFlags: CGEventFlags = []
private var heldKeyCodes: [Int: CGKeyCode] = [:]
private let modifierFlags: [Int: CGEventFlags] = [
    29: .maskControl, 157: .maskControl, 42: .maskShift, 54: .maskShift,
    56: .maskAlternate, 184: .maskAlternate
]

private func reply(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0a]))
}

private func error(_ message: String) { reply(["ok": false, "error": message]) }

@discardableResult
private func releaseAllHeldKeys() -> Bool {
    let keys = Set(heldKeyCodes.values)
    heldKeyCodes.removeAll()
    heldFlags = []

    // Cleanup must remain quick when Elite has already closed. Clear our
    // bookkeeping regardless, and post key-up events only when its window and
    // existing event permission are immediately available.
    guard !keys.isEmpty,
          let window = eliteWindow(waitingUpTo: 0),
          CGPreflightPostEventAccess() else { return false }
    for keyCode in keys {
        guard let event = CGEvent(
                keyboardEventSource: nil, virtualKey: keyCode, keyDown: false) else { continue }
        event.flags = []
        event.setIntegerValueField(.keyboardEventKeyboardType, value: 41)
        event.postToPid(window.pid)
    }
    return true
}

while let line = readLine() {
    guard let data = line.data(using: .utf8),
          let request = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let op = request["op"] as? String else {
        error("Malformed request")
        continue
    }

    if op == "quit" {
        releaseAllHeldKeys()
        reply(["ok": true])
        break
    }
    if op == "permissions" {
        reply(["ok": true, "postEvents": CGPreflightPostEventAccess(),
               "accessibility": AXIsProcessTrusted()])
        continue
    }
    if op == "releaseAll" {
        let posted = releaseAllHeldKeys()
        reply(["ok": true, "posted": posted])
        continue
    }
    if op == "key" {
        guard let scanCode = request["scanCode"] as? Int,
              let down = request["down"] as? Bool,
              let keyCode = macKeyCode[scanCode] else {
            error("Unsupported DirectInput scan code \(request["scanCode"] ?? "nil")")
            continue
        }
        var eventFlags = heldFlags
        if let flag = modifierFlags[scanCode] {
            if down { eventFlags.insert(flag) } else { eventFlags.remove(flag) }
        }
        guard let window = eliteWindow() else {
            error("Could not find visible Elite window named '\(wantedTitle)'")
            continue
        }
        guard CGPreflightPostEventAccess() || CGRequestPostEventAccess() else {
            error("Accessibility permission is required to send input to Elite")
            continue
        }
        guard let event = CGEvent(keyboardEventSource: nil, virtualKey: keyCode, keyDown: down) else {
            error("Could not create keyboard event")
            continue
        }
        event.flags = eventFlags
        event.setIntegerValueField(.keyboardEventKeyboardType, value: 41)
        event.postToPid(window.pid)
        heldFlags = eventFlags
        if down {
            heldKeyCodes[scanCode] = keyCode
        } else {
            heldKeyCodes.removeValue(forKey: scanCode)
        }
        reply(["ok": true, "pid": window.pid, "keyCode": keyCode])
        continue
    }
    guard let window = eliteWindow() else {
        error("Could not find visible Elite window named '\(wantedTitle)'")
        continue
    }
    if op == "window" {
        reply(["ok": true, "pid": window.pid, "owner": window.owner, "title": window.title,
               "x": window.bounds.origin.x, "y": window.bounds.origin.y,
               "width": window.bounds.width, "height": window.bounds.height])
        continue
    }
    if op == "focus" {
        guard let app = NSRunningApplication(processIdentifier: window.pid) else {
            error("Could not resolve Elite's owning process")
            continue
        }
        _ = app.activate(options: [.activateAllWindows])
        let axApp = AXUIElementCreateApplication(window.pid)
        let status = AXUIElementSetAttributeValue(
            axApp, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        if status == .success {
            reply(["ok": true])
        } else {
            error("Could not activate Elite's process (AX error \(status.rawValue))")
        }
        continue
    }
    error("Unknown operation '\(op)'")
}
