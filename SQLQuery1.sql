IF DB_ID(N'VehicleVisionDB') IS NULL
BEGIN
    CREATE DATABASE VehicleVisionDB;
END
GO

USE VehicleVisionDB;
GO

IF OBJECT_ID(N'dbo.Alerts', N'U') IS NOT NULL DROP TABLE dbo.Alerts;
IF OBJECT_ID(N'dbo.GestureRecords', N'U') IS NOT NULL DROP TABLE dbo.GestureRecords;
IF OBJECT_ID(N'dbo.LicensePlateRecords', N'U') IS NOT NULL DROP TABLE dbo.LicensePlateRecords;
IF OBJECT_ID(N'dbo.SystemLogs', N'U') IS NOT NULL DROP TABLE dbo.SystemLogs;
IF OBJECT_ID(N'dbo.WechatLoginSessions', N'U') IS NOT NULL DROP TABLE dbo.WechatLoginSessions;
IF OBJECT_ID(N'dbo.VerificationCodes', N'U') IS NOT NULL DROP TABLE dbo.VerificationCodes;
IF OBJECT_ID(N'dbo.Users', N'U') IS NOT NULL DROP TABLE dbo.Users;
IF OBJECT_ID(N'dbo.VehicleState', N'U') IS NOT NULL DROP TABLE dbo.VehicleState;
GO

CREATE TABLE dbo.Users (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Username NVARCHAR(64) NOT NULL,
    Email NVARCHAR(128) NULL,
    Phone NVARCHAR(20) NULL,
    email_encrypted NVARCHAR(512) NULL,
    email_lookup NVARCHAR(64) NULL,
    phone_encrypted NVARCHAR(256) NULL,
    phone_lookup NVARCHAR(64) NULL,
    HashedPassword NVARCHAR(256) NULL,
    IsActive BIT NOT NULL CONSTRAINT DF_Users_IsActive DEFAULT(1),
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_Users_CreatedAt DEFAULT(SYSDATETIME()),
    CONSTRAINT UQ_Users_Username UNIQUE (Username),
    CONSTRAINT UQ_Users_Email UNIQUE (Email),
    CONSTRAINT UQ_Users_Phone UNIQUE (Phone)
);
GO

CREATE TABLE dbo.VerificationCodes (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Target NVARCHAR(128) NOT NULL,
    Code NVARCHAR(8) NOT NULL,
    target_lookup NVARCHAR(64) NULL,
    code_hash NVARCHAR(64) NULL,
    Purpose NVARCHAR(32) NOT NULL CONSTRAINT DF_VerificationCodes_Purpose DEFAULT('login'),
    ExpiresAt DATETIME2 NOT NULL,
    Used BIT NOT NULL CONSTRAINT DF_VerificationCodes_Used DEFAULT(0)
);
GO

CREATE TABLE dbo.WechatLoginSessions (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    SessionId NVARCHAR(64) NOT NULL,
    Status NVARCHAR(16) NOT NULL CONSTRAINT DF_WechatLoginSessions_Status DEFAULT('pending'),
    UserId INT NULL,
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_WechatLoginSessions_CreatedAt DEFAULT(SYSDATETIME()),
    CONSTRAINT UQ_WechatLoginSessions_SessionId UNIQUE (SessionId),
    CONSTRAINT FK_WechatLoginSessions_Users FOREIGN KEY (UserId) REFERENCES dbo.Users(Id)
);
GO

CREATE TABLE dbo.SystemLogs (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Category NVARCHAR(64) NOT NULL,
    Level NVARCHAR(16) NOT NULL CONSTRAINT DF_SystemLogs_Level DEFAULT('INFO'),
    Message NVARCHAR(500) NOT NULL,
    DetailJson NVARCHAR(MAX) NULL,
    UserId INT NULL,
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_SystemLogs_CreatedAt DEFAULT(SYSDATETIME()),
    CONSTRAINT FK_SystemLogs_Users FOREIGN KEY (UserId) REFERENCES dbo.Users(Id)
);
GO

CREATE TABLE dbo.LicensePlateRecords (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    UserId INT NULL,
    SourceType NVARCHAR(32) NOT NULL,
    ImagePath NVARCHAR(500) NULL,
    AnnotatedImage NVARCHAR(MAX) NULL,
    PlatesJson NVARCHAR(MAX) NULL,
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_LicensePlateRecords_CreatedAt DEFAULT(SYSDATETIME()),
    CONSTRAINT FK_LicensePlateRecords_Users FOREIGN KEY (UserId) REFERENCES dbo.Users(Id)
);
GO

CREATE TABLE dbo.GestureRecords (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    UserId INT NULL,
    GestureType NVARCHAR(32) NOT NULL,
    GestureCode NVARCHAR(64) NULL,
    GestureCn NVARCHAR(64) NULL,
    Confidence FLOAT NOT NULL CONSTRAINT DF_GestureRecords_Confidence DEFAULT(0),
    ActionName NVARCHAR(64) NULL,
    AnnotatedImage NVARCHAR(MAX) NULL,
    KeypointsJson NVARCHAR(MAX) NULL,
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_GestureRecords_CreatedAt DEFAULT(SYSDATETIME()),
    CONSTRAINT FK_GestureRecords_Users FOREIGN KEY (UserId) REFERENCES dbo.Users(Id)
);
GO

CREATE TABLE dbo.Alerts (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Level NVARCHAR(16) NOT NULL,
    EventType NVARCHAR(64) NOT NULL,
    Title NVARCHAR(128) NOT NULL,
    Summary NVARCHAR(500) NOT NULL,
    RootCause NVARCHAR(500) NULL,
    Suggestion NVARCHAR(500) NULL,
    Status NVARCHAR(32) NOT NULL CONSTRAINT DF_Alerts_Status DEFAULT('open'),
    CreatedAt DATETIME2 NOT NULL CONSTRAINT DF_Alerts_CreatedAt DEFAULT(SYSDATETIME())
);
GO

CREATE TABLE dbo.VehicleState (
    Id INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    Volume INT NOT NULL CONSTRAINT DF_VehicleState_Volume DEFAULT(50),
    Temperature INT NOT NULL CONSTRAINT DF_VehicleState_Temperature DEFAULT(24),
    PhoneStatus NVARCHAR(32) NOT NULL CONSTRAINT DF_VehicleState_PhoneStatus DEFAULT('idle'),
    CurrentPage NVARCHAR(64) NOT NULL CONSTRAINT DF_VehicleState_CurrentPage DEFAULT('home'),
    IsAwake BIT NOT NULL CONSTRAINT DF_VehicleState_IsAwake DEFAULT(0),
    UpdatedAt DATETIME2 NOT NULL CONSTRAINT DF_VehicleState_UpdatedAt DEFAULT(SYSDATETIME())
);
GO

CREATE INDEX IX_SystemLogs_Category_CreatedAt ON dbo.SystemLogs(Category, CreatedAt DESC);
CREATE INDEX IX_LicensePlateRecords_UserId_CreatedAt ON dbo.LicensePlateRecords(UserId, CreatedAt DESC);
CREATE INDEX IX_GestureRecords_UserId_CreatedAt ON dbo.GestureRecords(UserId, CreatedAt DESC);
CREATE INDEX IX_Alerts_Level_CreatedAt ON dbo.Alerts(Level, CreatedAt DESC);
GO

INSERT INTO dbo.VehicleState (Volume, Temperature, PhoneStatus, CurrentPage, IsAwake)
VALUES (50, 24, N'idle', N'home', 0);
GO

INSERT INTO dbo.SystemLogs (Category, Level, Message, DetailJson)
VALUES
(N'system', N'INFO', N'数据库初始化完成', N'{"step":"init","result":"ok"}');
GO

INSERT INTO dbo.Alerts (Level, EventType, Title, Summary, RootCause, Suggestion, Status)
VALUES
(N'info', N'init', N'系统已初始化', N'数据库和基础数据已准备完成', N'首次部署', N'现在可以开始登录和识别测试', N'open');
GO
USE VehicleVisionDB;
GO

INSERT INTO dbo.Users (Username, Email, Phone, HashedPassword, IsActive)
VALUES
(N'admin', NULL, NULL, N'$2b$12$GUzAFCe47hUIvSfY7B3pC.h4IUD5etnsIwSJ4O34iUTPD1qrzf.hi', 1);
GO

USE VehicleVisionDB;
GO

IF EXISTS (
    SELECT 1
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c
        ON dc.parent_object_id = c.object_id
       AND dc.parent_column_id = c.column_id
    INNER JOIN sys.tables t
        ON t.object_id = dc.parent_object_id
    WHERE t.name = 'Users' AND c.name = 'is_active'
)
BEGIN
    DECLARE @sql1 NVARCHAR(MAX) = (
        SELECT 'ALTER TABLE dbo.Users DROP CONSTRAINT ' + dc.name
        FROM sys.default_constraints dc
        INNER JOIN sys.columns c
            ON dc.parent_object_id = c.object_id
           AND dc.parent_column_id = c.column_id
        INNER JOIN sys.tables t
            ON t.object_id = dc.parent_object_id
        WHERE t.name = 'Users' AND c.name = 'is_active'
    );
    EXEC sp_executesql @sql1;
END
GO

IF EXISTS (
    SELECT 1
    FROM sys.default_constraints dc
    INNER JOIN sys.columns c
        ON dc.parent_object_id = c.object_id
       AND dc.parent_column_id = c.column_id
    INNER JOIN sys.tables t
        ON t.object_id = dc.parent_object_id
    WHERE t.name = 'Users' AND c.name = 'created_at'
)
BEGIN
    DECLARE @sql2 NVARCHAR(MAX) = (
        SELECT 'ALTER TABLE dbo.Users DROP CONSTRAINT ' + dc.name
        FROM sys.default_constraints dc
        INNER JOIN sys.columns c
            ON dc.parent_object_id = c.object_id
           AND dc.parent_column_id = c.column_id
        INNER JOIN sys.tables t
            ON t.object_id = dc.parent_object_id
        WHERE t.name = 'Users' AND c.name = 'created_at'
    );
    EXEC sp_executesql @sql2;
END
GO

USE VehicleVisionDB;
GO

IF COL_LENGTH('dbo.Users', 'hashed_password') IS NULL
BEGIN
    ALTER TABLE dbo.Users ADD hashed_password NVARCHAR(256) NULL;
END
GO

IF COL_LENGTH('dbo.Users', 'is_active') IS NULL
BEGIN
    ALTER TABLE dbo.Users ADD is_active BIT NOT NULL CONSTRAINT DF_Users_IsActive DEFAULT(1);
END
GO

IF COL_LENGTH('dbo.Users', 'created_at') IS NULL
BEGIN
    ALTER TABLE dbo.Users ADD created_at DATETIME2 NOT NULL CONSTRAINT DF_Users_CreatedAt DEFAULT(SYSDATETIME());
END
GO

USE VehicleVisionDB;
GO

IF NOT EXISTS (SELECT 1 FROM dbo.Users WHERE username = 'admin')
BEGIN
    INSERT INTO dbo.Users (username, email, phone, hashed_password, is_active, created_at)
    VALUES
    (N'admin', NULL, NULL, N'$2b$12$GUzAFCe47hUIvSfY7B3pC.h4IUD5etnsIwSJ4O34iUTPD1qrzf.hi', 1, SYSDATETIME());
END
GO
