#import <AppKit/AppKit.h>
#import <unistd.h>

@interface EDAPOverlayView : NSView
@property(nonatomic, strong) NSDictionary *state;
@end

static NSColor *EDAPColor(NSArray *rgb) {
    if (![rgb isKindOfClass:[NSArray class]] || rgb.count < 3) {
        return NSColor.greenColor;
    }
    return [NSColor colorWithCalibratedRed:[rgb[0] doubleValue] / 255.0
                                      green:[rgb[1] doubleValue] / 255.0
                                       blue:[rgb[2] doubleValue] / 255.0
                                      alpha:1.0];
}

@implementation EDAPOverlayView
- (BOOL)isFlipped { return YES; }
- (BOOL)isOpaque { return NO; }

- (void)drawRect:(NSRect)dirtyRect {
    [NSColor.clearColor setFill];
    NSRectFillUsingOperation(dirtyRect, NSCompositingOperationCopy);
    NSDictionary *state = self.state;
    if (!state) return;

    for (NSArray *item in state[@"rectangles"] ?: @[]) {
        if (item.count < 4) continue;
        NSArray *a = item[0], *b = item[1];
        NSBezierPath *path = [NSBezierPath bezierPathWithRect:NSMakeRect(
            [a[0] doubleValue], [a[1] doubleValue],
            [b[0] doubleValue] - [a[0] doubleValue],
            [b[1] doubleValue] - [a[1] doubleValue])];
        path.lineWidth = MAX(1.0, [item[3] doubleValue]);
        [EDAPColor(item[2]) setStroke];
        [path stroke];
    }

    for (NSArray *item in state[@"quadrilaterals"] ?: @[]) {
        if (item.count < 3 || [item[0] count] < 4) continue;
        NSArray *points = item[0];
        NSBezierPath *path = [NSBezierPath bezierPath];
        [path moveToPoint:NSMakePoint(
            [points[0][0] doubleValue], [points[0][1] doubleValue])];
        for (NSUInteger index = 1; index < 4; index++) {
            [path lineToPoint:NSMakePoint(
                [points[index][0] doubleValue], [points[index][1] doubleValue])];
        }
        [path closePath];
        path.lineWidth = MAX(1.0, [item[2] doubleValue]);
        [EDAPColor(item[1]) setStroke];
        [path stroke];
    }

    NSArray *fontSpec = state[@"font"];
    CGFloat fontSize = fontSpec.count > 1 ? [fontSpec[1] doubleValue] : 14.0;
    NSString *fontName = fontSpec.count ? fontSpec[0] : @"Menlo";
    NSFont *font = [NSFont fontWithName:fontName size:fontSize]
        ?: [NSFont monospacedSystemFontOfSize:fontSize weight:NSFontWeightMedium];
    NSArray *origin = state[@"position"];
    CGFloat originX = origin.count > 0 ? [origin[0] doubleValue] : 0;
    CGFloat originY = origin.count > 1 ? [origin[1] doubleValue] : 0;

    for (NSArray *item in state[@"text"] ?: @[]) {
        if (item.count < 4) continue;
        NSDictionary *attributes = @{
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: EDAPColor(item[3])
        };
        NSPoint point = NSMakePoint(
            originX + [item[2] doubleValue] * fontSize,
            originY + [item[1] doubleValue] * fontSize);
        [[item[0] description] drawAtPoint:point withAttributes:attributes];
    }

    for (NSArray *item in state[@"floating_text"] ?: @[]) {
        if (item.count < 4) continue;
        NSDictionary *attributes = @{
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: EDAPColor(item[3])
        };
        [[item[0] description]
            drawAtPoint:NSMakePoint([item[1] doubleValue], [item[2] doubleValue])
            withAttributes:attributes];
    }
}
@end

@interface EDAPOverlayController : NSObject
@property(nonatomic, strong) NSPanel *window;
@property(nonatomic, strong) EDAPOverlayView *view;
@property(nonatomic, copy) NSString *statePath;
@property(nonatomic, strong) NSDate *lastModified;
@end

static EDAPOverlayController *EDAPOverlayControllerInstance;
static dispatch_source_t launcherMonitor;

@implementation EDAPOverlayController
- (instancetype)initWithPath:(NSString *)path {
    if ((self = [super init])) {
        _statePath = [path copy];
        _view = [[EDAPOverlayView alloc] initWithFrame:NSMakeRect(0, 0, 100, 100)];
        _window = [[NSPanel alloc]
            initWithContentRect:NSMakeRect(0, 0, 100, 100)
            styleMask:NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            backing:NSBackingStoreBuffered
            defer:NO];
        _window.contentView = _view;
        _window.opaque = NO;
        _window.backgroundColor = NSColor.clearColor;
        _window.hasShadow = NO;
        _window.ignoresMouseEvents = YES;
        _window.hidesOnDeactivate = NO;
        _window.becomesKeyOnlyIfNeeded = YES;
        _window.level = NSPopUpMenuWindowLevel;
        _window.collectionBehavior = NSWindowCollectionBehaviorCanJoinAllSpaces |
                                     NSWindowCollectionBehaviorFullScreenAuxiliary |
                                     NSWindowCollectionBehaviorStationary;
        [_window orderFrontRegardless];
        [NSTimer scheduledTimerWithTimeInterval:0.05
                                         target:self
                                       selector:@selector(refresh:)
                                       userInfo:nil
                                        repeats:YES];
    }
    return self;
}

- (void)refresh:(NSTimer *)timer {
    NSDictionary *attributes = [[NSFileManager defaultManager]
        attributesOfItemAtPath:self.statePath error:nil];
    NSDate *modified = attributes[NSFileModificationDate];
    if (!modified || (self.lastModified && [modified isEqualToDate:self.lastModified])) return;
    self.lastModified = modified;

    NSData *data = [NSData dataWithContentsOfFile:self.statePath];
    if (!data) return;
    NSDictionary *state = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if (![state isKindOfClass:[NSDictionary class]]) return;
    if ([state[@"quit"] boolValue]) {
        [NSApp terminate:nil];
        return;
    }

    NSArray *target = state[@"target"];
    if (target.count >= 4) {
        CGFloat x = [target[0] doubleValue];
        CGFloat top = [target[1] doubleValue];
        CGFloat width = [target[2] doubleValue];
        CGFloat height = [target[3] doubleValue];
        // CG global coordinates use the primary display's top-left origin;
        // AppKit uses that same display's bottom-left, even for other screens.
        CGFloat cocoaY = NSMaxY(NSScreen.screens.firstObject.frame) - top - height;
        NSRect frame = NSMakeRect(x, cocoaY, width, height);
        if (!NSEqualRects(self.window.frame, frame)) {
            [self.window setFrame:frame display:NO];
        }
    }

    self.view.state = state;
    [self.view setNeedsDisplay:YES];
}
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) return 2;
        pid_t launcherPID = getppid();
        if (launcherPID <= 1) return 0;
        launcherMonitor = dispatch_source_create(
            DISPATCH_SOURCE_TYPE_PROC, launcherPID, DISPATCH_PROC_EXIT, dispatch_get_main_queue());
        dispatch_source_set_event_handler(launcherMonitor, ^{ [NSApp terminate:nil]; });
        dispatch_resume(launcherMonitor);
        NSApplication *application = NSApplication.sharedApplication;
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        EDAPOverlayControllerInstance = [[EDAPOverlayController alloc]
            initWithPath:[NSString stringWithUTF8String:argv[1]]];
        [application run];
    }
    return 0;
}
