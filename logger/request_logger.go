package logger

import (
	"compress/gzip"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/QuantumNous/new-api/common"

	"github.com/bytedance/gopkg/util/gopool"
	"github.com/gin-gonic/gin"
)

const (
	defaultMaxFileSize  = 100 * 1024 * 1024 // 100MB
	defaultMaxAge       = 30                 // 保留30天
	defaultMaxBackups   = 10                 // 保留10个备份文件
	defaultCompressAge  = 1                  // 1天前的日志压缩
)

var (
	requestLogFile         *os.File
	requestLogWriter       io.Writer
	requestLogPath         string
	requestLogSize         int64
	requestLogDate         string
	setupRequestLogLock    sync.Mutex
	setupRequestLogWorking bool

	// 可配置参数（通过环境变量）
	maxFileSize  int64 = defaultMaxFileSize
	maxAge       int   = defaultMaxAge
	maxBackups   int   = defaultMaxBackups
	compressAge  int   = defaultCompressAge
)

// RequestLogEntry 请求日志条目结构
type RequestLogEntry struct {
	Timestamp     string `json:"timestamp"`
	RequestID     string `json:"request_id"`
	UserID        int    `json:"user_id,omitempty"`
	TokenName     string `json:"token_name,omitempty"`
	ChannelID     int    `json:"channel_id,omitempty"`
	ChannelName   string `json:"channel_name,omitempty"`
	Model         string `json:"model"`
	OriginalModel string `json:"original_model,omitempty"`
	Method        string `json:"method"`
	Path          string `json:"path"`
	RequestBody   string `json:"request_body"`
	ContentType   string `json:"content_type,omitempty"`
}

// init 初始化配置
func init() {
	// 从环境变量读取配置
	if val := os.Getenv("REQUEST_LOG_MAX_SIZE"); val != "" {
		if size, err := strconv.ParseInt(val, 10, 64); err == nil {
			maxFileSize = size * 1024 * 1024 // 转换为字节
		}
	}
	if val := os.Getenv("REQUEST_LOG_MAX_AGE"); val != "" {
		if age, err := strconv.Atoi(val); err == nil {
			maxAge = age
		}
	}
	if val := os.Getenv("REQUEST_LOG_MAX_BACKUPS"); val != "" {
		if backups, err := strconv.Atoi(val); err == nil {
			maxBackups = backups
		}
	}
	if val := os.Getenv("REQUEST_LOG_COMPRESS_AGE"); val != "" {
		if age, err := strconv.Atoi(val); err == nil {
			compressAge = age
		}
	}
}

// SetupRequestLogger 设置请求日志记录器
func SetupRequestLogger() {
	defer func() {
		setupRequestLogWorking = false
	}()

	if *common.LogDir == "" {
		return
	}

	ok := setupRequestLogLock.TryLock()
	if !ok {
		log.Println("setup request log is already working")
		return
	}
	defer setupRequestLogLock.Unlock()

	// 关闭旧文件
	if requestLogFile != nil {
		requestLogFile.Close()
	}

	// 创建请求日志目录
	requestLogDir := filepath.Join(*common.LogDir, "requests")
	err := os.MkdirAll(requestLogDir, 0755)
	if err != nil {
		log.Printf("failed to create request log directory: %s", err.Error())
		return
	}

	// 生成新的日志文件路径
	now := time.Now()
	requestLogDate = now.Format("20060102")
	requestLogPath = filepath.Join(requestLogDir, fmt.Sprintf("requests-%s.jsonl", now.Format("20060102-150405")))

	// 打开新日志文件
	fd, err := os.OpenFile(requestLogPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Printf("failed to open request log file: %s", err.Error())
		return
	}

	requestLogFile = fd
	requestLogWriter = fd

	// 获取当前文件大小
	if stat, err := fd.Stat(); err == nil {
		requestLogSize = stat.Size()
	}

	log.Printf("request logger initialized: %s", requestLogPath)

	// 异步执行日志维护任务
	gopool.Go(func() {
		maintainRequestLogs(requestLogDir)
	})
}

// LogRequest 记录请求信息
func LogRequest(c *gin.Context, requestBody []byte) {
	// 如果没有配置日志目录，则不记录
	if *common.LogDir == "" {
		return
	}

	// 初始化请求日志记录器
	if requestLogWriter == nil {
		SetupRequestLogger()
		if requestLogWriter == nil {
			return
		}
	}

	// 检查是否需要滚动日志
	if shouldRotate() {
		if !setupRequestLogWorking {
			setupRequestLogWorking = true
			gopool.Go(func() {
				SetupRequestLogger()
			})
		}
	}

	// 构建日志条目
	entry := RequestLogEntry{
		Timestamp:     time.Now().Format("2006-01-02 15:04:05.000"),
		RequestID:     c.GetString(common.RequestIdKey),
		UserID:        c.GetInt("id"),
		TokenName:     c.GetString("token_name"),
		ChannelID:     c.GetInt("channel_id"),
		ChannelName:   c.GetString("channel_name"),
		Model:         c.GetString("model"),
		OriginalModel: c.GetString("original_model"),
		Method:        c.Request.Method,
		Path:          c.Request.URL.Path,
		RequestBody:   string(requestBody),
		ContentType:   c.Request.Header.Get("Content-Type"),
	}

	// 序列化为JSON
	jsonData, err := json.Marshal(entry)
	if err != nil {
		log.Printf("failed to marshal request log entry: %s", err.Error())
		return
	}

	// 写入日志文件（每行一个JSON对象）
	setupRequestLogLock.Lock()
	n, err := fmt.Fprintf(requestLogWriter, "%s\n", jsonData)
	if err != nil {
		log.Printf("failed to write request log: %s", err.Error())
		setupRequestLogLock.Unlock()
		return
	}
	requestLogSize += int64(n)
	setupRequestLogLock.Unlock()
}

// shouldRotate 检查是否需要滚动日志
func shouldRotate() bool {
	// 按日期滚动（每天）
	today := time.Now().Format("20060102")
	if requestLogDate != today {
		return true
	}

	// 按文件大小滚动
	if requestLogSize >= maxFileSize {
		return true
	}

	return false
}

// maintainRequestLogs 维护日志文件（压缩和清理）
func maintainRequestLogs(logDir string) {
	// 获取所有日志文件
	files, err := filepath.Glob(filepath.Join(logDir, "requests-*.jsonl*"))
	if err != nil {
		log.Printf("failed to list request log files: %s", err.Error())
		return
	}

	if len(files) == 0 {
		return
	}

	// 按修改时间排序（最新的在前面）
	sort.Slice(files, func(i, j int) bool {
		statI, _ := os.Stat(files[i])
		statJ, _ := os.Stat(files[j])
		return statI.ModTime().After(statJ.ModTime())
	})

	now := time.Now()
	compressedCount := 0
	deletedCount := 0

	for i, file := range files {
		// 跳过当前正在写入的文件
		if file == requestLogPath {
			continue
		}

		stat, err := os.Stat(file)
		if err != nil {
			continue
		}

		age := now.Sub(stat.ModTime())
		ageDays := int(age.Hours() / 24)

		// 删除超过保留天数的日志
		if maxAge > 0 && ageDays > maxAge {
			if err := os.Remove(file); err == nil {
				deletedCount++
				log.Printf("deleted old request log: %s (age: %d days)", filepath.Base(file), ageDays)
			}
			continue
		}

		// 删除超过最大备份数量的日志（保留最新的）
		if maxBackups > 0 && i >= maxBackups {
			if err := os.Remove(file); err == nil {
				deletedCount++
				log.Printf("deleted excess request log: %s (exceeds max backups: %d)", filepath.Base(file), maxBackups)
			}
			continue
		}

		// 压缩超过指定天数的未压缩日志
		if compressAge > 0 && ageDays >= compressAge && !strings.HasSuffix(file, ".gz") {
			if err := compressLogFile(file); err == nil {
				compressedCount++
				log.Printf("compressed request log: %s (age: %d days)", filepath.Base(file), ageDays)
			} else {
				log.Printf("failed to compress request log %s: %s", filepath.Base(file), err.Error())
			}
		}
	}

	if compressedCount > 0 || deletedCount > 0 {
		log.Printf("request log maintenance completed: compressed=%d, deleted=%d", compressedCount, deletedCount)
	}
}

// compressLogFile 压缩日志文件
func compressLogFile(filename string) error {
	// 读取原文件
	source, err := os.Open(filename)
	if err != nil {
		return err
	}
	defer source.Close()

	// 创建压缩文件
	destPath := filename + ".gz"
	dest, err := os.Create(destPath)
	if err != nil {
		return err
	}
	defer dest.Close()

	// 使用gzip压缩
	gzWriter := gzip.NewWriter(dest)
	defer gzWriter.Close()

	// 复制数据
	_, err = io.Copy(gzWriter, source)
	if err != nil {
		os.Remove(destPath) // 清理失败的压缩文件
		return err
	}

	// 压缩成功后删除原文件
	if err := gzWriter.Close(); err != nil {
		os.Remove(destPath)
		return err
	}

	if err := dest.Close(); err != nil {
		os.Remove(destPath)
		return err
	}

	return os.Remove(filename)
}

// LogRequestFromContext 从gin.Context中提取请求体并记录
func LogRequestFromContext(ctx context.Context) {
	c, ok := ctx.(*gin.Context)
	if !ok {
		return
	}

	// 获取请求体
	requestBody, err := common.GetRequestBody(c)
	if err != nil {
		log.Printf("failed to get request body for logging: %s", err.Error())
		return
	}

	// 记录请求
	LogRequest(c, requestBody)
}
