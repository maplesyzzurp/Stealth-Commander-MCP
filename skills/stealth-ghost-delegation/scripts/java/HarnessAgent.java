import java.awt.Component;
import java.awt.Container;
import java.awt.Dialog;
import java.awt.EventQueue;
import java.awt.Frame;
import java.awt.Window;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Properties;
import java.util.concurrent.atomic.AtomicReference;
import javax.swing.AbstractButton;
import javax.swing.JComboBox;
import javax.swing.JComponent;
import javax.swing.JLabel;
import javax.swing.text.JTextComponent;

public final class HarnessAgent {
    private HarnessAgent() {}

    public static void agentmain(String filename, Instrumentation instrumentation) {
        Properties command = new Properties();
        Path output = null;
        try {
            try (var reader = Files.newBufferedReader(Path.of(filename))) {
                command.load(reader);
            }
            String outputName = command.getProperty("output");
            if (outputName == null || outputName.isBlank()) {
                throw new IllegalArgumentException("Command is missing the output property");
            }
            output = Path.of(outputName);
            String result = onEdt(() -> perform(command, instrumentation));
            write(output, "{\"ok\":true,\"result\":\"" + jsonEscape(result) + "\"}");
        } catch (Throwable failure) {
            if (output != null) {
                try {
                    write(output, "{\"ok\":false,\"result\":\"\",\"error\":\""
                            + jsonEscape(message(failure)) + "\"}");
                } catch (Exception ignored) {
                    failure.printStackTrace();
                }
            } else {
                failure.printStackTrace();
            }
        }
    }

    private static String perform(Properties command, Instrumentation instrumentation) throws Exception {
        String action = required(command, "action");
        switch (action) {
            case "dump":
                return dump();
            case "click":
                return click(command);
            case "toggle":
                return setSelected(command);
            case "text":
                return setText(command);
            case "trust-project":
                return trustProject(command);
            case "dismiss-tour":
                return dismissTour();
            case "roots":
                return roots(instrumentation);
            case "open":
                return open(command, instrumentation);
            case "select":
                return select(command, instrumentation);
            case "run":
                return run(instrumentation);
            case "input":
                return input(command, instrumentation);
            case "content":
                return content(command, instrumentation);
            case "layout":
                return layout();
            default:
                throw new IllegalArgumentException("Unknown action: " + action);
        }
    }

    private static String dump() {
        List<Component> controls = visibleControls();
        StringBuilder result = new StringBuilder();
        for (int i = 0; i < controls.size(); i++) {
            Component control = controls.get(i);
            result.append(i).append(" | ").append(control.getClass().getName())
                    .append(" | ").append(label(control))
                    .append(" | enabled=").append(control.isEnabled())
                    .append(control instanceof AbstractButton button
                            ? " | selected=" + button.isSelected() : "")
                    .append(" | ").append(control.getBounds()).append('\n');
        }
        return result.toString();
    }

    private static String click(Properties command) throws Exception {
        Component control = indexedControl(command);
        checkExpectedLabel(command, control);
        if (!control.isEnabled()) throw new IllegalStateException("Control is disabled");
        if (control instanceof AbstractButton button) button.doClick();
        else control.getClass().getMethod("click").invoke(control);
        return "Activated " + label(control);
    }

    private static String setText(Properties command) {
        Component control = indexedControl(command);
        checkExpectedLabel(command, control);
        if (!(control instanceof JTextComponent text)) {
            throw new IllegalArgumentException("Indexed control is not a text component");
        }
        text.setText(required(command, "text"));
        return "Set text in " + control.getClass().getName();
    }

    private static String setSelected(Properties command) {
        Component control = indexedControl(command);
        checkExpectedLabel(command, control);
        if (command.getProperty("expectedLabel") == null || command.getProperty("expectedLabel").isBlank()) {
            throw new IllegalArgumentException("Toggle requires expectedLabel from a fresh dump");
        }
        if (!(control instanceof AbstractButton button)) {
            throw new IllegalArgumentException("Indexed control is not a button or checkbox");
        }
        String selected = required(command, "selected");
        if (!selected.equalsIgnoreCase("true") && !selected.equalsIgnoreCase("false")) {
            throw new IllegalArgumentException("selected must be true or false");
        }
        button.setSelected(Boolean.parseBoolean(selected));
        return "Set selected=" + button.isSelected() + " for " + label(button);
    }

    private static String trustProject(Properties command) throws Exception {
        Path sessionRoot = Path.of(required(command, "sessionRoot")).toRealPath();
        Path projectRoot = Path.of(required(command, "projectRoot")).toRealPath();
        if (!projectRoot.startsWith(sessionRoot)) {
            throw new IllegalArgumentException("Project root is outside the current private session");
        }

        List<Dialog> dialogs = new ArrayList<>();
        for (Window window : Window.getWindows()) {
            if (window instanceof Dialog dialog && dialog.isShowing()) {
                List<Component> visible = new ArrayList<>();
                walk(dialog, visible);
                for (Component control : visible) {
                    if (control instanceof AbstractButton button && "Trust Project".equals(button.getText())) {
                        dialogs.add(dialog);
                        break;
                    }
                }
            }
        }
        if (dialogs.size() != 1) {
            throw new IllegalStateException("Expected one visible Trust Project dialog, found " + dialogs.size());
        }

        Dialog dialog = dialogs.get(0);
        List<Component> controls = new ArrayList<>();
        walk(dialog, controls);
        AbstractButton defender = uniqueButton(controls, "microsoft defender", "Microsoft Defender option");
        AbstractButton folderTrust = uniqueFolderTrustButton(controls);
        AbstractButton trust = uniqueActionButton(controls, "Trust Project");
        uniqueActionButton(controls, "Preview in Safe Mode");
        uniqueActionButton(controls, "Don't Open");
        if (!label(folderTrust).contains(sessionRoot.getFileName().toString())) {
            throw new IllegalStateException("Trust dialog does not name this private session folder");
        }
        folderTrust.setSelected(false);
        defender.setSelected(false);
        if (folderTrust.isSelected() || defender.isSelected()) {
            throw new IllegalStateException("Could not disable folder trust and Microsoft Defender exclusion");
        }
        if (!trust.isEnabled()) throw new IllegalStateException("Trust Project action is disabled");
        trust.doClick();
        return "Trusted project " + projectRoot + " with folder trust and Microsoft Defender exclusion disabled";
    }

    private static AbstractButton uniqueButton(List<Component> controls, String text, String description) {
        List<AbstractButton> matches = new ArrayList<>();
        for (Component control : controls) {
            if (control instanceof AbstractButton button && label(button).toLowerCase(java.util.Locale.ROOT)
                    .contains(text)) matches.add(button);
        }
        if (matches.size() != 1) {
            throw new IllegalStateException("Expected one " + description + ", found " + matches.size());
        }
        return matches.get(0);
    }

    private static AbstractButton uniqueFolderTrustButton(List<Component> controls) {
        List<AbstractButton> matches = new ArrayList<>();
        for (Component control : controls) {
            if (!(control instanceof AbstractButton button)) continue;
            String text = label(button).toLowerCase(java.util.Locale.ROOT);
            if (text.contains("trust all") || (text.contains("folder") && text.contains("trust"))) {
                matches.add(button);
            }
        }
        if (matches.size() != 1) {
            throw new IllegalStateException("Expected one folder trust option, found " + matches.size());
        }
        return matches.get(0);
    }

    private static AbstractButton uniqueActionButton(List<Component> controls, String text) {
        List<AbstractButton> matches = new ArrayList<>();
        for (Component control : controls) {
            if (control instanceof AbstractButton button && button.getText() != null
                    && button.getText().trim().equalsIgnoreCase(text)) matches.add(button);
        }
        if (matches.size() != 1) {
            throw new IllegalStateException("Expected one " + text + " button, found " + matches.size());
        }
        return matches.get(0);
    }

    private static Component indexedControl(Properties command) {
        if (command.getProperty("index") == null) {
            String expected = required(command, "expectedLabel");
            List<Component> matches = new ArrayList<>();
            for (Component control : visibleControls()) {
                if (label(control).equals(expected)) matches.add(control);
            }
            if (matches.size() != 1) throw new IllegalArgumentException("Expected one control with label '" + expected + "', found " + matches.size());
            return matches.get(0);
        }
        int index;
        try {
            index = Integer.parseInt(required(command, "index"));
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException("Control index must be an integer", e);
        }
        List<Component> controls = visibleControls();
        if (index < 0 || index >= controls.size()) {
            throw new IllegalArgumentException("Control index is outside the current dump");
        }
        Component control = controls.get(index);
        if (!control.isShowing()) throw new IllegalStateException("Control is no longer showing");
        return control;
    }

    private static String dismissTour() {
        List<Dialog> matches = new ArrayList<>();
        for (Window window : Window.getWindows()) {
            if (!(window instanceof Dialog dialog) || !dialog.isShowing()) continue;
            List<Component> controls = new ArrayList<>();
            walk(dialog, controls);
            for (Component control : controls) {
                if (control instanceof JLabel title && "Meet the Islands Theme".equals(title.getText())) {
                    matches.add(dialog);
                    break;
                }
            }
        }
        if (matches.size() != 1) throw new IllegalStateException("Expected the known theme-tour dialog");
        List<Component> controls = new ArrayList<>();
        walk(matches.get(0), controls);
        uniqueActionButton(controls, "Start Quick Tour");
        uniqueActionButton(controls, "Skip").doClick();
        return "Skipped the introductory theme tour";
    }

    private static void checkExpectedLabel(Properties command, Component control) {
        String expected = command.getProperty("expectedLabel");
        if (expected != null && !expected.equals(label(control))) {
            throw new IllegalStateException("Control label changed; expected '" + expected
                    + "' but found '" + label(control) + "'");
        }
    }

    private static List<Component> visibleControls() {
        List<Component> controls = new ArrayList<>();
        for (Window window : Window.getWindows()) walk(window, controls);
        return controls;
    }

    private static void walk(Component component, List<Component> controls) {
        if (!component.isShowing()) return;
        controls.add(component);
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) walk(child, controls);
        }
    }

    private static String label(Component component) {
        String text = "";
        if (component instanceof AbstractButton button) text = button.getText();
        else if (component instanceof JLabel label) text = label.getText();
        else if (component instanceof JTextComponent field) text = field.getText();
        else if (component instanceof JComboBox<?> box) text = String.valueOf(box.getSelectedItem());
        else if (component.getAccessibleContext() != null) {
            text = component.getAccessibleContext().getAccessibleName();
        }
        if (text == null) text = "";
        text = text.replace('\n', ' ').replace('\r', ' ');
        if (text.length() > 240) text = text.substring(0, 240);
        if (component instanceof JComponent jc && jc.getToolTipText() != null) {
            text += " [" + jc.getToolTipText() + "]";
        }
        return text;
    }

    private static String roots(Instrumentation instrumentation) throws Exception {
        Object project = project(instrumentation);
        Object manager = call(ideClass(instrumentation, "com.intellij.openapi.module.ModuleManager"),
                "getInstance", project);
        StringBuilder result = new StringBuilder();
        for (Object module : (Object[]) call(manager, "getModules")) {
            Object rootManager = call(ideClass(instrumentation,
                    "com.intellij.openapi.roots.ModuleRootManager"), "getInstance", module);
            result.append(call(module, "getName")).append('\n');
            for (Object root : (Object[]) call(rootManager, "getContentRoots")) {
                result.append(call(root, "getPath")).append('\n');
            }
        }
        return result.toString();
    }

    private static String open(Properties command, Instrumentation instrumentation) throws Exception {
        Path projectRoot = Path.of(required(command, "projectRoot")).toRealPath();
        Path requested = Path.of(required(command, "path")).toRealPath();
        if (!requested.startsWith(projectRoot)) {
            throw new IllegalArgumentException("Requested path is outside the expected project root");
        }
        Object project = project(instrumentation);
        Object editors = call(ideClass(instrumentation,
                "com.intellij.openapi.fileEditor.FileEditorManager"), "getInstance", project);
        for (Object file : (Object[]) call(editors, "getOpenFiles")) call(editors, "closeFile", file);
        Object localFileSystem = call(ideClass(instrumentation,
                "com.intellij.openapi.vfs.LocalFileSystem"), "getInstance");
        Object file = call(localFileSystem, "findFileByPath", requested.toString());
        if (file == null) throw new IllegalArgumentException("File is not available in the IDE: " + requested);
        call(editors, "openFile", file, true);
        return "Opened " + requested;
    }

    private static String select(Properties command, Instrumentation instrumentation) throws Exception {
        Object project = project(instrumentation);
        Object manager = call(ideClass(instrumentation, "com.intellij.execution.RunManager"),
                "getInstance", project);
        String requested = required(command, "name");
        List<String> names = new ArrayList<>();
        for (Object setting : (List<?>) call(manager, "getAllSettings")) {
            String name = String.valueOf(call(setting, "getName"));
            names.add(name);
            if (name.equals(requested)) {
                call(manager, "setSelectedConfiguration", setting);
                return "Selected " + name;
            }
        }
        throw new IllegalArgumentException("Run configuration not found: " + requested
                + ". Available: " + String.join(", ", names));
    }

    private static String run(Instrumentation instrumentation) throws Exception {
        Object project = project(instrumentation);
        Object manager = call(ideClass(instrumentation, "com.intellij.execution.RunManager"),
                "getInstance", project);
        Object setting = call(manager, "getSelectedConfiguration");
        if (setting == null) throw new IllegalStateException("No run configuration is selected");
        if (setting == null) throw new IllegalStateException("No run configuration is selected");
        ClassLoader loader = ideClass(instrumentation,
                "com.intellij.openapi.project.ProjectManager").getClassLoader();
        Class<?> projectType = Class.forName("com.intellij.openapi.project.Project", true, loader);
        Class<?> settingsType = Class.forName("com.intellij.execution.RunnerAndConfigurationSettings", true, loader);
        Class<?> executorType = Class.forName("com.intellij.execution.Executor", true, loader);
        Object executor = call(Class.forName("com.intellij.execution.executors.DefaultRunExecutor",
                true, loader), "getRunExecutorInstance");
        Class<?> utility = Class.forName("com.intellij.execution.ProgramRunnerUtil", true, loader);
        Method execute = utility.getMethod("executeConfiguration", projectType, settingsType, executorType);
        execute.invoke(null, project, setting, executor);
        return "Requested run configuration " + call(setting, "getName") + "; inspect content for process output";
    }

    private static String input(Properties command, Instrumentation instrumentation) throws Exception {
        Object project = project(instrumentation);
        Object execution = call(ideClass(instrumentation, "com.intellij.execution.ExecutionManager"),
                "getInstance", project);
        Object contentManager = call(execution, "getContentManager");
        Object content = call(contentManager, "getSelectedContent");
        if (content == null) throw new IllegalStateException("No run console is selected");
        Object console = call(content, "getExecutionConsole");
        String text = required(command, "text");
        if (!text.endsWith("\n")) text += "\n";
        call(console, "type", call(console, "getEditor"), text);
        return "Entered console input";
    }

    private static String content(Properties command, Instrumentation instrumentation) throws Exception {
        Object project = project(instrumentation);
        Object manager = call(ideClass(instrumentation,
                "com.intellij.execution.ui.RunContentManager"), "getInstance", project);
        String requested = command.getProperty("name");
        List<String> names = new ArrayList<>();
        for (Object descriptor : (List<?>) call(manager, "getAllDescriptors")) {
            String name = String.valueOf(call(descriptor, "getDisplayName"));
            names.add(name);
            if (requested == null || requested.equals(name)) {
                call(manager, "selectRunContent", descriptor);
                Object console = call(descriptor, "getExecutionConsole");
                Object process = call(descriptor, "getProcessHandler");
                Object exitCode = process == null ? null : call(process, "getExitCode");
                return name + "\n" + call(console, "getText") + "\nExit code: " + exitCode;
            }
        }
        throw new IllegalArgumentException("Run content not found: " + requested
                + ". Available: " + String.join(", ", names));
    }

    private static String layout() throws Exception {
        StringBuilder result = new StringBuilder();
        for (Window window : Window.getWindows()) arrange(window, result);
        return result.toString();
    }

    private static void arrange(Component component, StringBuilder result) throws Exception {
        if (!component.isShowing()) return;
        if (component.getClass().getName().equals("com.intellij.openapi.ui.ThreeComponentsSplitter")
                && component.getWidth() > 500) {
            boolean vertical = (Boolean) call(component, "getOrientation");
            call(component, vertical ? "setLastSize" : "setFirstSize", vertical ? 450 : 250);
            result.append("Adjusted ").append(vertical ? "Run panel" : "Project panel").append('\n');
        }
        if (component.getClass().getName().equals("com.intellij.ui.BalloonImpl$MyComponent")) {
            for (var field : component.getClass().getDeclaredFields()) {
                if (field.getType().getName().equals("com.intellij.ui.BalloonImpl")) {
                    field.setAccessible(true);
                    Object balloon = field.get(component);
                    call(balloon, "hide");
                    result.append("Dismissed notification\n");
                }
            }
        }
        if (component instanceof Container container) {
            for (Component child : container.getComponents()) arrange(child, result);
        }
    }

    private static Object project(Instrumentation instrumentation) throws Exception {
        Object manager = call(ideClass(instrumentation, "com.intellij.openapi.project.ProjectManager"),
                "getInstance");
        Object[] projects = (Object[]) call(manager, "getOpenProjects");
        if (projects.length == 0) throw new IllegalStateException("No project is open");
        return projects[0];
    }

    private static Class<?> ideClass(Instrumentation instrumentation, String name) throws ClassNotFoundException {
        ClassLoader loader = null;
        for (Class<?> type : instrumentation.getAllLoadedClasses()) {
            if (type.getName().equals(name)) return type;
            if (type.getName().equals("com.intellij.openapi.project.ProjectManager")) {
                loader = type.getClassLoader();
            }
        }
        if (loader != null) return Class.forName(name, true, loader);
        throw new ClassNotFoundException("IDE class is not loaded and its class loader is unavailable: " + name);
    }

    private static Object call(Object target, String name, Object... args) throws Exception {
        Class<?> type = target instanceof Class<?> cls ? cls : target.getClass();
        for (Method method : type.getMethods()) {
            if (!method.getName().equals(name) || method.getParameterCount() != args.length) continue;
            Class<?>[] parameters = method.getParameterTypes();
            boolean matches = true;
            for (int i = 0; i < args.length; i++) {
                Class<?> parameter = parameters[i];
                if (parameter.isPrimitive()) parameter = primitiveWrapper(parameter);
                if (args[i] != null && !parameter.isInstance(args[i])) matches = false;
            }
            if (matches) {
                method.setAccessible(true);
                return method.invoke(target instanceof Class<?> ? null : target, args);
            }
        }
        throw new NoSuchMethodException(type.getName() + "." + name + "/" + args.length);
    }

    private static Class<?> primitiveWrapper(Class<?> type) {
        if (type == boolean.class) return Boolean.class;
        if (type == int.class) return Integer.class;
        if (type == long.class) return Long.class;
        if (type == double.class) return Double.class;
        if (type == float.class) return Float.class;
        if (type == short.class) return Short.class;
        if (type == byte.class) return Byte.class;
        if (type == char.class) return Character.class;
        return type;
    }

    private static String onEdt(ThrowingSupplier<String> supplier) throws Exception {
        if (EventQueue.isDispatchThread()) return supplier.get();
        AtomicReference<String> result = new AtomicReference<>();
        AtomicReference<Throwable> failure = new AtomicReference<>();
        EventQueue.invokeAndWait(() -> {
            try {
                result.set(supplier.get());
            } catch (Throwable e) {
                failure.set(e);
            }
        });
        if (failure.get() != null) {
            Throwable error = failure.get();
            if (error instanceof Exception exception) throw exception;
            throw new RuntimeException(error);
        }
        return result.get();
    }

    private static String required(Properties command, String name) {
        String value = command.getProperty(name);
        if (value == null || value.isBlank()) throw new IllegalArgumentException("Command is missing " + name);
        return value;
    }

    private static String message(Throwable failure) {
        Throwable cause = failure;
        while (cause.getCause() != null && cause.getCause() != cause) cause = cause.getCause();
        String text = cause.getClass().getName() + ": " + cause.getMessage();
        for (StackTraceElement line : cause.getStackTrace()) text += "\n" + line;
        return text;
    }

    private static void write(Path path, String value) throws Exception {
        Path absolute = path.toAbsolutePath().normalize();
        Path parent = absolute.getParent();
        if (parent != null) Files.createDirectories(parent);
        Files.writeString(absolute, value);
    }

    private static String jsonEscape(String value) {
        StringBuilder escaped = new StringBuilder();
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            switch (c) {
                case '"': escaped.append("\\\""); break;
                case '\\': escaped.append("\\\\"); break;
                case '\b': escaped.append("\\b"); break;
                case '\f': escaped.append("\\f"); break;
                case '\n': escaped.append("\\n"); break;
                case '\r': escaped.append("\\r"); break;
                case '\t': escaped.append("\\t"); break;
                default:
                    if (c < 0x20) escaped.append(String.format("\\u%04x", (int) c));
                    else escaped.append(c);
            }
        }
        return escaped.toString();
    }

    @FunctionalInterface
    private interface ThrowingSupplier<T> {
        T get() throws Exception;
    }
}
