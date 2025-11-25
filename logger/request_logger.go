package logger

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/QuantumNous/new-api/common"

	"github.com/bytedance/gopkg/util/gopool"
	"github.com/gin-gonic/gin"
)

const maxRequestLogCount = 500000

var requestLogCount int
var setupRequestLogLock sync.Mutex
var setupRequestLogWorking bool
var requestLogWriter io.Writer

// RequestLogEntry 请求日志条目结构
type RequestLogEntry struct {
	Timestamp     string                 `json:"timestamp"`
	RequestID     string                 `json:"request_id"`
	UserID        int                    `json:"user_id,omitempty"`
	TokenName     string                 `json:"token_name,omitempty"`
	ChannelID     int                    `json:"channel_id,omitempty"`
	ChannelName   string                 `json:"channel_name,omitempty"`
	Model         string                 `json:"model"`
	OriginalModel string                 `json:"original_model,omitempty"`
	Method        string                 `json:"method"`
	Path          string                 `json:"path"`
	RequestBody   string                 `json:"request_body"`
	ContentType   string                 `json:"content_type,omitempty"`
}

// SetupRequestLogger 设置请求日志记录器
func SetupRequestLogger() {
	defer func() {
		setupRequestLogWorking = false
	}()
	if *common.LogDir != "" {
		ok := setupRequestLogLock.TryLock()
		if !ok {
			log.Println("setup request log is already working")
			return
		}
		defer func() {
			setupRequestLogLock.Unlock()
		}()

		// 创建请求日志目录
		requestLogDir := filepath.Join(*common.LogDir, "requests")
		err := os.MkdirAll(requestLogDir, 0755)
		if err != nil {
			log.Printf("failed to create request log directory: %s", err.Error())
			return
		}

		logPath := filepath.Join(requestLogDir, fmt.Sprintf("requests-%s.jsonl", time.Now().Format("20060102150405")))
		fd, err := os.OpenFile(logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err != nil {
			log.Printf("failed to open request log file: %s", err.Error())
			return
		}
		requestLogWriter = fd
		log.Printf("request logger initialized: %s", logPath)
	}
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
	_, err = fmt.Fprintf(requestLogWriter, "%s\n", jsonData)
	if err != nil {
		log.Printf("failed to write request log: %s", err.Error())
		return
	}

	requestLogCount++
	// 当日志数量达到阈值时，轮转日志文件
	if requestLogCount > maxRequestLogCount && !setupRequestLogWorking {
		requestLogCount = 0
		setupRequestLogWorking = true
		gopool.Go(func() {
			SetupRequestLogger()
		})
	}
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
