-- The installer endpoint prepends the bridge URL and computer name.
local component = require("component")
local computer = require("computer")
local fs = require("filesystem")
local shell = require("shell")
local thread = require("thread")
local buffer = require("buffer")
local uuid = require("uuid")

local frameLimit = 131072
local outputLimit = 8192
local agent, activeRequest
local statusMessage = "stopped"

local function errorText(reason)
  if type(reason) == "table" then reason = reason.reason end
  return tostring(reason or "OpenOS operation failed")
end

local function pack(...)
  local parts = {...}
  for index, part in ipairs(parts) do
    parts[index] = part:gsub("%%", "%%25"):gsub("[^ -~]", function(byte)
      return string.format("%%%02X", byte:byte())
    end)
  end
  return table.concat(parts, "\t")
end

local function unpackFrame(frame)
  local fields = {}
  for part in (frame .. "\t"):gmatch("([^\t]*)\t") do
    fields[#fields + 1] = part:gsub("%%(%x%x)", function(hex)
      return string.char(tonumber(hex, 16))
    end)
  end
  return fields
end

local function pathOf(path)
  assert(#path > 0 and not path:find("\0", 1, true), "invalid path")
  return fs.canonical(path:sub(1, 1) == "/" and path or fs.concat("/home", path))
end

local function writeBytes(path, mode, data)
  local file = assert(fs.open(path, mode))
  local ok, written, reason = pcall(file.write, file, data)
  local closed, closeResult, closeReason = pcall(file.close, file)
  assert(ok, errorText(written))
  assert(written, reason or "failed to write file")
  assert(closed and closeResult ~= false and not closeReason, errorText(closeReason or closeResult))
end

local function replaceFile(path, data)
  local temporary = fs.concat(fs.path(path), ".oc-mcp-" .. uuid.next())
  local ok, reason = pcall(function()
    writeBytes(temporary, "wb", data)
    local moved, moveReason = fs.rename(temporary, path)
    if moved then return end
    -- Some OpenComputers filesystems cannot rename onto an existing file.
    assert(fs.exists(path) and not fs.isDirectory(path), moveReason or "failed to install file")
    local backup = temporary .. ".bak"
    assert(fs.rename(path, backup))
    local replaced, replaceReason = fs.rename(temporary, path)
    if not replaced then
      assert(fs.rename(backup, path), "could not restore the original file at " .. backup)
      error(replaceReason or "could not replace file", 0)
    end
    fs.remove(backup)
  end)
  if fs.exists(temporary) then fs.remove(temporary) end
  assert(ok, errorText(reason))
end

local operations = {}

function operations.list(path, offset)
  path = pathOf(path)
  assert(fs.isDirectory(path), "directory not found: " .. path)
  local result = {path, "0"}
  local remaining = tonumber(offset)
  for name in assert(fs.list(path)) do
    if remaining > 0 then
      remaining = remaining - 1
    elseif #result < 102 then
      result[#result + 1] = name
    else
      result[2] = "1"
      break
    end
  end
  return table.unpack(result)
end

function operations.read(path, offset, limit)
  path = pathOf(path)
  local position = tonumber(offset)
  local size = fs.size(path)
  local file = assert(fs.open(path, "rb"))
  local ok, data = pcall(function()
    if position >= size then return "" end
    assert(file:seek("set", position))
    local chunks, remaining = {}, tonumber(limit)
    while remaining > 0 do
      local chunk, reason = file:read(math.min(remaining, 2048))
      assert(not reason, reason)
      if not chunk or #chunk == 0 then break end
      chunks[#chunks + 1] = chunk
      remaining = remaining - #chunk
    end
    return table.concat(chunks)
  end)
  file:close()
  assert(ok, errorText(data))
  return path, data, (position + #data >= size or #data < tonumber(limit)) and "1" or "0"
end

function operations.write(path, mode, data)
  path = assert(fs.realPath(pathOf(path)))
  assert(not fs.isDirectory(path), "path is a directory: " .. path)
  if mode == "append" then
    writeBytes(path, "ab", data)
  else
    replaceFile(path, data)
  end
  return path
end

local function capture()
  local chunks, size, truncated = {}, 0, false
  local file = buffer.new("w", {
    write = function(_, data)
      local length = math.min(#data, outputLimit - size)
      if length > 0 then
        chunks[#chunks + 1] = data:sub(1, length)
        size = size + length
      end
      truncated = truncated or length < #data
      return true
    end,
    close = function() return true end,
  })
  file:setvbuf("no")
  return file, function() return table.concat(chunks), truncated end
end

function operations.exec(command, cwd, stdin)
  cwd = pathOf(cwd)
  assert(fs.isDirectory(cwd), "working directory not found: " .. cwd)
  shell.setWorkingDirectory(cwd)
  local stdout, readOut = capture()
  local stderr, readErr = capture()
  local position = 1
  local input = buffer.new("r", {
    read = function(_, count)
      if position > #stdin then return nil end
      local data = stdin:sub(position, position + count - 1)
      position = position + #data
      return data
    end,
    close = function() return true end,
  })
  io.input(input)
  io.output(stdout)
  io.error(stderr)
  local ok, result, reason = pcall(shell.execute, command)
  local exitCode = 0
  if not ok then
    exitCode = type(result) == "table" and tonumber(result.code) or 1
    stderr:write(errorText(result), "\n")
  elseif type(result) == "number" then
    exitCode = result
  elseif not result then
    exitCode = reason and 1 or require("sh").getLastExitCode()
    if exitCode == 0 then exitCode = 1 end
    if reason then stderr:write(errorText(reason), "\n") end
  end
  local out, outTruncated = readOut()
  local err, errTruncated = readErr()
  return "exit", tostring(exitCode), out, err, (outTruncated or errTruncated) and "1" or "0"
end

local function primaryGpu()
  assert(component.isAvailable("gpu"), "no GPU installed")
  local gpu = component.gpu
  local screen = assert(gpu.getScreen(), "no screen bound to the primary GPU")
  return gpu, screen
end

function operations.screen(row, count)
  local gpu = primaryGpu()
  -- Screen cells are buffer zero on GPUs with hardware buffers.
  assert(not gpu.getActiveBuffer or gpu.getActiveBuffer() == 0, "the primary GPU is currently drawing into an off-screen buffer")
  local width, height = gpu.getResolution()
  local first = tonumber(row)
  assert(first <= height, "row is outside the screen")
  local lines = {}
  for y = first, math.min(height, first + tonumber(count) - 1) do
    local cells = {}
    for x = 1, width do cells[x] = gpu.get(x, y) or " " end
    lines[#lines + 1] = table.concat(cells):gsub(" +$", "")
    if y % 4 == 0 then os.sleep(0.01) end
  end
  return tostring(width), tostring(height), table.concat(lines, "\n")
end

function operations.input(text, key)
  local _, screen = primaryGpu()
  local address = assert(component.invoke(screen, "getKeyboards")[1], "no keyboard attached to the screen")
  local keys = require("keyboard").keys
  local function signal(kind, value, code)
    local sent
    if code then
      sent = computer.pushSignal(kind, address, value, code, "codex")
    else
      sent = computer.pushSignal(kind, address, value, "codex")
    end
    assert(sent, "OpenOS signal queue is full")
  end
  local function press(char, code)
    signal("key_down", char, code)
    signal("key_up", char, code)
  end
  if #text > 0 then signal("clipboard", text) end
  if key == "ctrl+c" then
    signal("key_down", 0, keys.lcontrol)
    press(99, keys.c)
    signal("key_up", 0, keys.lcontrol)
  elseif #key > 0 then
    local codes = {backspace = keys.back, escape = 1}
    local chars = {enter = 13, tab = 9, backspace = 8, escape = 27}
    press(chars[key] or 0, assert(codes[key] or keys[key], "unknown key"))
  end
end

local function run(frame)
  local fields = unpackFrame(frame)
  local id, operation, timeout = fields[1], fields[2], tonumber(fields[3])
  local execute = operations[operation]
  if not execute or not timeout then return pack(id, "error", "invalid operation") end
  local result
  local worker = thread.create(function()
    result = table.pack(pcall(execute, table.unpack(fields, 4)))
  end)
  local deadline = computer.uptime() + timeout
  while not result and worker:status() == "running" and computer.uptime() < deadline do os.sleep(0.05) end
  if not result then
    worker:kill()
    if operation == "exec" then return pack(id, "ok", "timeout", "", "", "Command timed out", "0") end
    return pack(id, "error", "OpenOS operation timed out")
  end
  if not result[1] then return pack(id, "error", errorText(result[2]):sub(1, 1024)) end
  local answer = pack(id, "ok", table.unpack(result, 2, result.n))
  if #answer + 37 > frameLimit then return pack(id, "error", "result too large, request fewer bytes or rows") end
  return answer
end

local function poll(body)
  assert(component.isAvailable("internet"), "no Internet Card installed")
  activeRequest = assert(component.internet.request(endpoint, body))
  local ok, response = pcall(function()
    local chunks, size = {}, 0
    local deadline = computer.uptime() + 8
    repeat
      local chunk, reason = activeRequest.read(4096)
      assert(not reason, reason)
      if not chunk then return table.concat(chunks) end
      if #chunk > 0 then
        size = size + #chunk
        assert(size <= frameLimit, "bridge response too large")
        chunks[#chunks + 1] = chunk
      else
        os.sleep(0.05)
      end
    until computer.uptime() >= deadline
    error("bridge request timed out", 0)
  end)
  pcall(activeRequest.close)
  activeRequest = nil
  assert(ok, errorText(response))
  return response
end

local function loop()
  local address = computer.address()
  local result
  while true do
    local ok, frame = pcall(poll, result and (address .. "\t" .. result) or address)
    if ok then
      statusMessage = "connected"
      result = nil
      if frame ~= "idle" and #frame > 0 then
        result = run(frame)
        os.sleep(0.1)
      else
        os.sleep(idleDelay or 0.1)
      end
    else
      statusMessage = errorText(frame)
      os.sleep(2)
    end
  end
end

function start()
  if agent and agent:status() == "running" then return end
  assert(endpoint, "download the installer from the bridge")
  assert(component.isAvailable("internet"), "install an Internet Card first")
  statusMessage = "connecting"
  agent = thread.create(loop)
  assert(agent:detach())
end

function stop()
  if agent then agent:kill() end
  if activeRequest then pcall(activeRequest.close) end
  agent, activeRequest = nil, nil
  statusMessage = "stopped"
end

function status()
  print(computerName .. " (" .. computer.address() .. "): " .. statusMessage)
end

if ... == "install" then
  local source = assert(io.open("/tmp/oc-mcp.lua", "rb"))
  local script = assert(source:read("*a"))
  source:close()
  replaceFile("/etc/rc.d/oc-mcp.lua", script)
  local rc = require("rc")
  local old = rc.loaded["oc-mcp"]
  if old and old.stop then old.stop() end
  rc.loaded["oc-mcp"] = nil
  local config = {}
  assert(loadfile("/etc/rc.cfg", "t", config))()
  local enabled = false
  for _, name in ipairs(config.enabled or {}) do enabled = enabled or name == "oc-mcp" end
  if not enabled then
    assert(shell.execute("rc oc-mcp enable"), "could not enable oc-mcp")
  end
  assert(shell.execute("rc oc-mcp start"), "could not start oc-mcp")
  fs.remove("/tmp/oc-mcp.lua")
  print("Installed oc-mcp as " .. computerName .. ". Run 'rc oc-mcp status' to check the connection.")
end
