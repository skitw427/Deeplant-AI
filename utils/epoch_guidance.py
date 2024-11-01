# train, val 1 epoch를 진행하는 함수
# train 모드에서는 train과 validation 진행
# evaluate 모드에서는 validation만 진행
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from tqdm import tqdm
import mlflow
from sklearn.metrics import r2_score

def feature_guidance_loss(student_output, teacher_output, student_features, teacher_features, labels, alpha=0.5, temperature=2.0, beta=0.1):
    # 학생 모델의 출력을 타겟 크기에 맞게 조정
    student_output_resized = student_output[:, :5]  # 처음 5개의 출력만 사용
    
    # 주 손실 (MSE)
    main_loss = F.mse_loss(student_output_resized, labels)
    
    # 소프트 타겟 손실 (KL divergence)
    # 교사 모델의 출력도 같은 크기로 조정
    teacher_output_resized = teacher_output[:, :5]
    soft_loss = nn.KLDivLoss(reduction="batchmean")(
        F.log_softmax(student_output_resized / temperature, dim=1),
        F.softmax(teacher_output_resized / temperature, dim=1)
    ) * (temperature ** 2)
    
    # 특징 맵 손실
    feature_loss = 0
    for sf, tf in zip(student_features, teacher_features):
        # 특징 맵의 크기가 다를 수 있으므로, 필요하다면 여기서 크기를 조정해야 함
        feature_loss += F.mse_loss(sf, tf)
    
    return main_loss + alpha * soft_loss + beta * feature_loss

def regression(model, params):
    num_epochs = params['num_epochs']
    optimizer = params['optimizer']
    train_loader = params['train_dl']
    val_loader = params['val_dl']
    scheduler = params['scheduler']
    save_model = params['save_model']
    loss_func = params['loss_func']
    fold, n_folds = params['fold']
    label_names = params['label_names']

    print("save model:", save_model)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    best_val_loss = float('inf')
    train_losses, train_r2, val_losses, val_r2, val_acc, average_val_acc = [], [], [], [], [], []

    for epoch in range(num_epochs):
        print(f"\n{'-'*15} Epoch {epoch+1}/{num_epochs} {'-'*15}", end='')
        if fold:
            print(f" (Fold {fold}/{n_folds})\n")
        else:
            print()

        ###### Train ######
        model.train()
        running_loss = 0.0
        all_outputs = []
        all_labels = []
        for images, labels in tqdm(train_loader):
            images, labels = images.to(device), labels.to(device).float()
            
            optimizer.zero_grad()
            student_output, teacher_output, student_features, teacher_features = model(images)
            loss = feature_guidance_loss(student_output, teacher_output, student_features, teacher_features, labels)
            
            loss.backward()
            optimizer.step()

            running_loss += loss.item()
            all_outputs.append(student_output.cpu().detach().numpy())
            all_labels.append(labels.cpu().detach().numpy())

        all_outputs = np.concatenate(all_outputs, axis=0)
        all_labels = np.concatenate(all_labels, axis=0)

        train_loss = running_loss / len(train_loader)
        train_r2_value = r2_score(all_labels, all_outputs, multioutput='uniform_average')
        train_accuracies = calculate_accuracy(all_labels, all_outputs, [1.0])
        average_train_accuracies = np.mean(train_accuracies)

        train_losses.append(train_loss)
        train_r2.append(train_r2_value)

        mlflow.log_metric('train_loss', train_loss, step=epoch+1)
        mlflow.log_metric('train_R2', train_r2_value, step=epoch+1)

        print(f"\nEpoch {epoch+1} Summary:")
        if fold:
            print(f"Fold: {fold}/{n_folds}")
        print(f"Train Loss: {train_loss:.3f}")
        print(f"Train R2: {train_r2_value:.3f}")
        print(f"Train Accuracies (±1.0): {train_accuracies}")
        print(f"Average Train Accuracy (±1.0): {average_train_accuracies:.3f}")

        ###### Validation ######
        model.eval()
        val_loss = 0.0
        all_val_outputs = []
        all_val_labels = []
        with torch.no_grad():
            for images, labels in val_loader:
                images, labels = images.to(device), labels.to(device).float()
                    
                student_output, teacher_output, student_features, teacher_features = model(images)
                loss = feature_guidance_loss(student_output, teacher_output, student_features, teacher_features, labels)

                val_loss += loss.item()
                all_val_outputs.append(student_output.cpu().detach().numpy())
                all_val_labels.append(labels.cpu().detach().numpy())

            scheduler.step(val_loss)

            val_loss /= len(val_loader)
            all_val_outputs = np.concatenate(all_val_outputs, axis=0)
            all_val_labels = np.concatenate(all_val_labels, axis=0)
            val_r2_value = r2_score(all_val_labels, all_val_outputs, multioutput='uniform_average')
            val_accuracies_05 = calculate_accuracy(all_val_labels, all_val_outputs, [0.5])
            val_accuracies_10 = calculate_accuracy(all_val_labels, all_val_outputs, [1.0])
            val_accuracies_20 = calculate_accuracy(all_val_labels, all_val_outputs, [2.0])
            average_val_accuracies_05 = np.mean(val_accuracies_05)
            average_val_accuracies_10 = np.mean(val_accuracies_10)
            average_val_accuracies_20 = np.mean(val_accuracies_20)

            mlflow.log_metric('val_loss', val_loss, step=epoch + 1)
            mlflow.log_metric('val_R2', val_r2_value, step=epoch + 1)
            mlflow.log_metric('average_val_acc_05', average_val_accuracies_05, step=epoch + 1)
            mlflow.log_metric('average_val_acc_10', average_val_accuracies_10, step=epoch + 1)
            mlflow.log_metric('average_val_acc_20', average_val_accuracies_20, step=epoch + 1)

            for i, (acc_05, acc_10, acc_20) in enumerate(zip(val_accuracies_05, val_accuracies_10, val_accuracies_20)):
                label_name = label_names[i]
                mlflow.log_metric(f'val_acc_05_{label_name}', acc_05, step=epoch + 1)
                mlflow.log_metric(f'val_acc_10_{label_name}', acc_10, step=epoch + 1)
                mlflow.log_metric(f'val_acc_20_{label_name}', acc_20, step=epoch + 1)

            if val_loss < best_val_loss and save_model:
                best_val_loss = val_loss
                mlflow.pytorch.log_model(model, "best_model")
                print("model saved")

            val_losses.append(val_loss)
            val_r2.append(val_r2_value)
            val_acc.append((val_accuracies_05, val_accuracies_10, val_accuracies_20))
            average_val_acc.append((average_val_accuracies_05, average_val_accuracies_10, average_val_accuracies_20))

            print(f"\nValidation Results:")
            if fold:
                print(f"Fold: {fold}/{n_folds}")
            print(f"Epoch: {epoch+1}/{num_epochs}")
            print(f"Validation Loss: {val_loss:.3f}")
            print(f"Validation R2: {val_r2_value:.3f}")
            print(f"Validation Accuracies (±0.5): {val_accuracies_05}")
            print(f"Validation Accuracies (±1.0): {val_accuracies_10}")
            print(f"Validation Accuracies (±2.0): {val_accuracies_20}")
            print(f"Average Validation Accuracy (±0.5): {average_val_accuracies_05:.3f}")
            print(f"Average Validation Accuracy (±1.0): {average_val_accuracies_10:.3f}")
            print(f"Average Validation Accuracy (±2.0): {average_val_accuracies_20:.3f}")

    return [train_losses, train_r2, val_losses, val_r2, val_acc, average_val_acc]


# 정확도 계산
def calculate_accuracy(labels, outputs, tolerances):
    accuracies = []
    for tol in tolerances:
        acc_per_tol = []
        for i in range(labels.shape[1]):
            correct = ((labels[:, i] - tol) <= outputs[:, i]) & (outputs[:, i] <= (labels[:, i] + tol))
            accuracy = correct.sum() / len(labels)
            acc_per_tol.append(accuracy)
        accuracies.append(acc_per_tol)
    return accuracies[0] if len(tolerances) == 1 else accuracies